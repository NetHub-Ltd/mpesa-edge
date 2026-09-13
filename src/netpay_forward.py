"""
Forward queued M-Pesa envelopes to NetPay internal ingest.

Security model:
- Only this Worker (queue consumer) calls NetPay /internal/events.
- Authentication: shared secret NETPAY_INTERNAL_API_KEY → header X-Internal-Api-Key.
- NETPAY_BASE_URL must be the NetPay API origin (no trailing slash), not the public SPA if separate.
- Secrets must be set via `wrangler secret put` — never committed.
"""
from __future__ import annotations

import json
from typing import Any


class ForwardConfigError(RuntimeError):
    pass


def _require_env(env: Any, name: str) -> str:
    value = getattr(env, name, None)
    if value is None or str(value).strip() == "":
        raise ForwardConfigError(f"Missing Worker secret/binding: {name}")
    return str(value).rstrip("/")


async def forward_envelope_to_netpay(env: Any, envelope: dict[str, Any] | str) -> tuple[int, str]:
    """
    POST envelope to NetPay POST /internal/events.
    Returns (status_code, response_text). Raises on transport/config failure so the queue can retry.
    """
    base = _require_env(env, "NETPAY_BASE_URL")
    api_key = _require_env(env, "NETPAY_INTERNAL_API_KEY")
    url = f"{base}/internal/events"

    if isinstance(envelope, str):
        body = envelope
        # Ensure JSON object for NetPay
        try:
            json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Queue message is not valid JSON: {exc}") from exc
    else:
        # Normalize integration id → public_id for NetPay resolvers
        integ = envelope.get("integration") or {}
        if isinstance(integ, dict) and "public_id" not in integ and integ.get("id"):
            integ = {**integ, "public_id": integ["id"]}
            envelope = {**envelope, "integration": integ}
        body = json.dumps(envelope)

    headers = {
        "Content-Type": "application/json",
        "X-Internal-Api-Key": api_key,
        "User-Agent": "mpesa-edge/netpay-forward",
    }

    # Cloudflare Python Workers: workers.fetch
    try:
        from workers import fetch as cf_fetch
    except ImportError:
        cf_fetch = None

    if cf_fetch is not None:
        response = await cf_fetch(
            url,
            method="POST",
            headers=headers,
            body=body,
        )
        text = await response.text()
        status = int(response.status)
    else:
        # Local / fallback (Pyodide)
        from pyodide.http import pyfetch

        response = await pyfetch(
            url,
            method="POST",
            headers=headers,
            body=body,
        )
        text = await response.text()
        status = int(response.status)

    # 2xx: success. 4xx (except 408/429): do not infinite-retry poison messages — still raise for visibility
    # NetPay returns 401 on bad key — should not ack without fixing secrets.
    if status >= 500 or status in (408, 429):
        raise RuntimeError(f"NetPay temporary error {status}: {text[:300]}")
    if status == 401 or status == 403:
        raise RuntimeError(f"NetPay auth failed {status}: check NETPAY_INTERNAL_API_KEY")
    if status >= 400:
        raise RuntimeError(f"NetPay rejected envelope {status}: {text[:300]}")
    return status, text


async def send_heartbeat(env: Any, *, source: str = "cron") -> tuple[int, str]:
    """POST a non-financial edge.heartbeat envelope to NetPay."""
    import time
    import uuid

    event_id = f"hb_{int(time.time())}_{uuid.uuid4().hex[:10]}"
    envelope = {
        "event_id": event_id,
        "provider": "mpesa",
        "event_type": "edge.heartbeat",
        "integration": {
            "id": "gw_edge_heartbeat",
            "public_id": "gw_edge_heartbeat",
            "type": "edge",
        },
        "received_at": None,
        "request": {"method": "HEARTBEAT", "path": "/internal/heartbeat"},
        "payload": {"source": source, "worker": "mpesa-edge"},
    }
    return await forward_envelope_to_netpay(env, envelope)
