"""
Callback routing, event-envelope construction and validation
for the NetHub M-Pesa Gateway Worker.

Design rule:
    We never validate the raw M-Pesa / Safaricom body.
    We only guarantee that the envelope WE produce is complete
    and well-formed before it is sent to the queue.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
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
    type: str = "unknown"  # will later become "paybill" | "till" | ...


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

    if envelope.event_type not in ("c2b_validation", "c2b_confirmation"):
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

    Only checks method, path shape and the opaque integration_id.
    """

    ALLOWED_EVENTS = {"validation", "confirmation"}

    EVENT_TYPE_MAP = {
        "validation": "c2b_validation",
        "confirmation": "c2b_confirmation",
    }

    def __init__(self, request):
        self.request = request
        self.integration_id: Optional[str] = None
        self.event_type: Optional[str] = None  # short name from URL
        self.canonical_event_type: Optional[str] = None  # c2b_* form
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
        parts = path.split("/")

        # Expected: ['', 'mpesa', 'cb', '{integration_id}', '{event_type}']
        if len(parts) != 5 or parts[1] != "mpesa" or parts[2] != "cb":
            self.error_response = Response("Not Found", status=404)
            return False

        integration_id = parts[3]
        event_type = parts[4]

        if event_type not in self.ALLOWED_EVENTS:
            self.error_response = Response("Not Found", status=404)
            return False

        if not integration_id or not integration_id.startswith("gw_"):
            self.error_response = Response("Invalid integration identifier", status=400)
            return False

        self.integration_id = integration_id
        self.event_type = event_type
        self.canonical_event_type = self.EVENT_TYPE_MAP[event_type]
        return True


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
