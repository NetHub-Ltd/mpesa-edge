"""
Cloudflare Worker entrypoint for the NetHub M-Pesa Gateway.

This file stays deliberately thin.
All routing, envelope construction and validation live in worker.py.
Queue consumer forwards envelopes to NetPay (netpay_forward.py).
"""

import json

from workers import Response, WorkerEntrypoint
from worker import CallbackRouter, EnvelopeBuilder, validate_envelope
from netpay_forward import ForwardConfigError, forward_envelope_to_netpay


class Default(WorkerEntrypoint):
    """Cloudflare Worker entrypoint (HTTP + Queue)."""

    async def fetch(self, request):
        """
        Ingest Safaricom callbacks → normalize → queue.
        """
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
