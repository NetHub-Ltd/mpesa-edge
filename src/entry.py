"""
Cloudflare Worker entrypoint for the NetHub M-Pesa Gateway.

This file stays deliberately thin.
All routing, envelope construction and validation live in worker.py.
Queue consumer forwards envelopes to NetPay (netpay_forward.py).
"""

import json

from workers import Response, WorkerEntrypoint
from worker import CallbackRouter, EnvelopeBuilder, validate_envelope
from netpay_forward import ForwardConfigError, forward_envelope_to_netpay, send_heartbeat


class Default(WorkerEntrypoint):
    """Cloudflare Worker entrypoint (HTTP + Queue)."""

    async def fetch(self, request):
        """
        Ingest Safaricom callbacks → normalize → queue.
        Manual: GET/POST /__netpay/ping with header X-Edge-Admin-Key (optional secret).
        """
        url = str(getattr(request, "url", ""))
        path = ""
        try:
            from urllib.parse import urlparse
            path = urlparse(url).path
        except Exception:
            path = url

        if path.rstrip("/").endswith("/__netpay/ping") or path.endswith("__netpay/ping"):
            admin = getattr(self.env, "EDGE_ADMIN_KEY", None) or getattr(self.env, "NETPAY_INTERNAL_API_KEY", None)
            provided = None
            try:
                provided = request.headers.get("X-Edge-Admin-Key")
            except Exception:
                provided = None
            if not admin or provided != str(admin):
                return Response("Unauthorized", status=401)
            try:
                status, text = await send_heartbeat(self.env, source="manual-ping")
                return Response(
                    json.dumps({"ok": status < 300, "netpay_status": status, "body": text[:500]}),
                    status=200 if status < 300 else 502,
                    headers={"Content-Type": "application/json"},
                )
            except Exception as exc:
                return Response(
                    json.dumps({"ok": False, "error": str(exc)}),
                    status=502,
                    headers={"Content-Type": "application/json"},
                )

        router = CallbackRouter(request)

        if not router.parse():
            return router.error_response

        raw_body = await request.text()
        envelope = EnvelopeBuilder(router, raw_body).build()
        error = validate_envelope(envelope)
        if error:
            print(f"Envelope validation failed: {error}")
            return Response(f"Internal envelope error: {error}", status=500)

        if router.is_validation:
            try:
                await self.env.MPESA_QUEUE.send(json.dumps(envelope.to_dict()))
            except Exception as exc:
                print(f"Queue error (validation): {exc}")

            return Response(
                json.dumps(
                    {
                        "ResultCode": "0",
                        "ResultDesc": "Accepted",
                    }
                ),
                status=200,
                headers={"Content-Type": "application/json"},
            )

        try:
            await self.env.MPESA_QUEUE.send(json.dumps(envelope.to_dict()))
            return Response(
                f"Queued successfully ({router.event_type}) for {router.integration_id}",
                status=202,
            )
        except Exception as exc:
            print(f"Queue error: {exc}")
            return Response("Failed to queue callback", status=500)

    async def queue(self, batch):
        """
        Consume mpesa-callbacks and POST each envelope to NetPay /internal/events.

        Requires secrets:
          - NETPAY_BASE_URL           e.g. https://api.nethub.co.ke
          - NETPAY_INTERNAL_API_KEY   same value as NetPay INTERNAL_API_KEY
        """
        for message in batch.messages:
            try:
                body = message.body
                if isinstance(body, dict):
                    envelope = body
                else:
                    envelope = json.loads(body) if isinstance(body, str) else body

                status, text = await forward_envelope_to_netpay(self.env, envelope)
                eid = envelope.get("event_id") if isinstance(envelope, dict) else "?"
                print(f"Forwarded event_id={eid} status={status} body={text[:200]}")
                # Success: message is acked when handler completes without throw
            except ForwardConfigError as exc:
                print(f"Config error (will retry until secrets set): {exc}")
                raise
            except Exception as exc:
                print(f"Forward failed (queue will retry): {exc}")
                raise

    async def scheduled(self, controller):
        """Periodic live check that NetPay accepts edge traffic."""
        try:
            status, text = await send_heartbeat(self.env, source="cron")
            print(f"Heartbeat cron status={status} body={text[:200]}")
        except Exception as exc:
            print(f"Heartbeat cron failed: {exc}")
            raise
