"""
Callback routing, event-envelope construction and validation
for the NetHub M-Pesa Gateway Worker.

Design rules:
- We never validate the raw M-Pesa / Safaricom body.
- We only guarantee that the envelope WE produce is complete.
- C2B Validation is the only route that requires an immediate
  synchronous decision. Everything else is queued.
- Backend (FastAPI + PostgreSQL) owns all idempotency and
  financial effects.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import uuid4

from workers import Response

# ---------------------------------------------------------------------------
# Envelope schema (the only thing we validate)
# ---------------------------------------------------------------------------


@dataclass
class IntegrationInfo:
    """Minimal integration metadata that travels with every event."""

    id: str
    type: str = "unknown"  # later: "paybill" | "till" | ...


@dataclass
class RequestInfo:
    """HTTP request metadata captured at the edge."""

    method: str
    path: str


@dataclass
class EventEnvelope:
    """
    Normalized gateway event that is written to the queue.

    The original provider payload is stored under `payload` and is
    never inspected or mutated by the Worker.
    """

    event_id: str
    provider: str
    event_type: str
    integration: IntegrationInfo
    received_at: str
    request: RequestInfo
    payload: Any  # original body (dict or str) – we do not validate it

    def to_dict(self) -> dict:
        """Convert to a plain dict ready for json.dumps."""
        return asdict(self)


def validate_envelope(envelope: EventEnvelope) -> Optional[str]:
    """
    Pure-Python validation of the envelope we just built.

    Returns:
        None  → envelope is valid
        str   → human-readable error message
    """
    if not envelope.event_id or not envelope.event_id.startswith("evt_"):
        return "event_id is missing or malformed"

    if envelope.provider != "mpesa":
        return "provider must be 'mpesa'"

    allowed = {
        "c2b_validation",
        "c2b_confirmation",
        "stk_callback",
        "b2c_result",
        "b2c_timeout",
    }
    if envelope.event_type not in allowed:
        return f"unsupported event_type: {envelope.event_type}"

    if not envelope.integration.id or not envelope.integration.id.startswith("gw_"):
        return "integration.id is missing or malformed"

    if not envelope.received_at:
        return "received_at is missing"

    if not envelope.request.method or not envelope.request.path:
        return "request metadata is incomplete"

    # payload may be anything – we deliberately do not touch it
    return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class CallbackRouter:
    """
    Parses and validates incoming M-Pesa callback requests.

    Supported routes (under the same opaque integration ID):
        /mpesa/cb/{integration_id}/validation
        /mpesa/cb/{integration_id}/confirmation
        /mpesa/cb/{integration_id}/stk
        /mpesa/cb/{integration_id}/b2c-result
        /mpesa/cb/{integration_id}/b2c-timeout
    """

    # short name from URL  →  canonical event_type used in the envelope
    EVENT_TYPE_MAP = {
        "validation": "c2b_validation",
        "confirmation": "c2b_confirmation",
        "stk": "stk_callback",
        "b2c-result": "b2c_result",
        "b2c-timeout": "b2c_timeout",
    }

    def __init__(self, request):
        self.request = request
        self.integration_id: Optional[str] = None
        self.event_type: Optional[str] = None  # short name from URL
        self.canonical_event_type: Optional[str] = None  # c2b_*, stk_*, b2c_*
        self.error_response: Optional[Response] = None

    def parse(self) -> bool:
        """
        Returns True when the request is valid for further processing.
        """
        if self.request.method != "POST":
            self.error_response = Response("Method Not Allowed", status=405)
            return False

        # request.url is a plain string in Cloudflare Python Workers
        parsed = urlparse(self.request.url)
        path = parsed.path.rstrip("/")
        parts = [p for p in path.split("/") if p != ""]

        # Preferred: /cb/{integration_id}/{event_type}
        # Legacy:    /mpesa/cb/{integration_id}/{event_type}
        integration_id = None
        event_type = None
        if len(parts) == 3 and parts[0] == "cb":
            integration_id, event_type = parts[1], parts[2]
        elif len(parts) == 4 and parts[0] == "mpesa" and parts[1] == "cb":
            integration_id, event_type = parts[2], parts[3]
        else:
            self.error_response = Response("Not Found", status=404)
            return False

        if event_type not in self.EVENT_TYPE_MAP:
            self.error_response = Response("Not Found", status=404)
            return False

        if not integration_id or not integration_id.startswith("gw_"):
            self.error_response = Response("Invalid integration identifier", status=400)
            return False

        self.integration_id = integration_id
        self.event_type = event_type
        self.canonical_event_type = self.EVENT_TYPE_MAP[event_type]
        return True

    @property
    def is_validation(self) -> bool:
        """True when this is a C2B Validation request that needs an immediate decision."""
        return self.event_type == "validation"


# ---------------------------------------------------------------------------
# Envelope builder
# ---------------------------------------------------------------------------


class EnvelopeBuilder:
    """
    Constructs a validated EventEnvelope from a successful router parse
    and the raw request body.
    """

    def __init__(self, router: CallbackRouter, raw_body: str):
        self.router = router
        self.raw_body = raw_body

    def build(self) -> EventEnvelope:
        # Keep the original body as structured JSON when possible.
        # If it is not valid JSON we store it as a plain string.
        # We never raise or reject on this step.
        try:
            import json

            payload: Any = json.loads(self.raw_body)
        except Exception:
            payload = self.raw_body

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        return EventEnvelope(
            event_id=f"evt_{uuid4().hex}",
            provider="mpesa",
            event_type=self.router.canonical_event_type,
            integration=IntegrationInfo(id=self.router.integration_id),
            received_at=now,
            request=RequestInfo(
                method=self.router.request.method,
                path=urlparse(self.router.request.url).path,
            ),
            payload=payload,
        )
