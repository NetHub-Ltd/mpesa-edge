"""
Cloudflare Worker entrypoint for the NetHub M-Pesa Gateway.

This file stays deliberately thin.
All routing / validation logic lives in worker.py.
"""

from workers import Response, WorkerEntrypoint
from worker import CallbackRouter  # import from the sibling file


class Default(WorkerEntrypoint):
    """
    Cloudflare Worker entrypoint.

    The runtime looks for a class that inherits from WorkerEntrypoint
    and calls its `fetch` method for every incoming HTTP request.
    """

    async def fetch(self, request):
        """
        Main request handler.

        Flow:
        1. Let CallbackRouter validate method + path + integration_id
        2. If invalid → return the error response immediately
        3. Read the raw body
        4. Send the raw body to the Cloudflare Queue (MPESA_QUEUE)
        5. Return 202 Accepted (or 500 if the queue send fails)
        """
        # --- Step A: Route & validate ---
        router = CallbackRouter(request)

        if not router.parse():
            # Early return with the appropriate 4xx response
            return router.error_response

        # --- Step B: Read the original payload (we keep it untouched) ---
        body = await request.text()

        # --- Step C: Enqueue for asynchronous processing ---
        try:
            # MPESA_QUEUE is bound in wrangler.jsonc / Cloudflare dashboard
            await self.env.MPESA_QUEUE.send(body)

            return Response(
                f"Queued successfully ({router.event_type}) for {router.integration_id}",
                status=202,
            )

        except Exception as exc:
            # Log the error so it appears in Cloudflare Worker logs
            print(f"Queue error: {exc}")
            return Response("Failed to queue callback", status=500)
