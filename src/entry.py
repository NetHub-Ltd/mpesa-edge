"""
Cloudflare Worker entrypoint for the NetHub M-Pesa Gateway.

This file stays deliberately thin.
All routing, envelope construction and validation live in worker.py.
"""

import json

from workers import Response, WorkerEntrypoint
from worker import CallbackRouter, EnvelopeBuilder, validate_envelope


class Default(WorkerEntrypoint):
    """
    Cloudflare Worker entrypoint.
    """

    async def fetch(self, request):
        """
        Main request handler.

        Flow:
        1. Validate method + path + integration_id
        2. Read the raw body (never validated)
        3. Build the normalized envelope
        4. Validate the envelope we just built
        5. Send the envelope to the queue
        6. Return 202 Accepted
        """
        # --- Step A: Route & validate request shape ---
        router = CallbackRouter(request)

        if not router.parse():
            return router.error_response

        # --- Step B: Read original payload (untouched) ---
        raw_body = await request.text()

        # --- Step C: Build envelope ---
        envelope = EnvelopeBuilder(router, raw_body).build()

        # --- Step D: Validate only our envelope ---
        error = validate_envelope(envelope)
        if error:
            # This should never happen if EnvelopeBuilder is correct,
            # but we guard it so a programming error cannot poison the queue.
            print(f"Envelope validation failed: {error}")
            return Response(f"Internal envelope error: {error}", status=500)

        # --- Step E: Enqueue the validated envelope ---
        try:
            await self.env.MPESA_QUEUE.send(json.dumps(envelope.to_dict()))

            return Response(
                f"Queued successfully ({router.event_type}) for {router.integration_id}",
                status=202,
            )

        except Exception as exc:
            print(f"Queue error: {exc}")
            return Response("Failed to queue callback", status=500)
