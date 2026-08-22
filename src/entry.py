"""
Cloudflare Worker entrypoint for the NetHub M-Pesa Gateway.

This file stays deliberately thin.
All routing / validation / envelope logic lives in worker.py.
"""

import json

from workers import Response, WorkerEntrypoint
from worker import CallbackRouter, EventEnvelope


class Default(WorkerEntrypoint):
    """
    Cloudflare Worker entrypoint.
    """

    async def fetch(self, request):
        """
        Main request handler.

        Flow:
        1. Validate method + path + integration_id
        2. Read the raw body
        3. Build the normalized event envelope
        4. Send the envelope (as JSON) to the queue
        5. Return 202 Accepted
        """
        # --- Step A: Route & validate ---
        router = CallbackRouter(request)

        if not router.parse():
            return router.error_response

        # --- Step B: Read original payload ---
        raw_body = await request.text()

        # --- Step C: Build normalized envelope ---
        envelope = EventEnvelope(router, raw_body).build()

        # --- Step D: Enqueue the envelope ---
        try:
            await self.env.MPESA_QUEUE.send(json.dumps(envelope))

            return Response(
                f"Queued successfully ({router.event_type}) for {router.integration_id}",
                status=202,
            )

        except Exception as exc:
            print(f"Queue error: {exc}")
            return Response("Failed to queue callback", status=500)
