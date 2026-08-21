from workers import Response, WorkerEntrypoint


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        # Only accept POST callbacks for now.
        if request.method != "POST":
            return Response("Method Not Allowed", status=405)

        body = await request.text()

        # Send the raw callback payload to Cloudflare Queue.
        await self.env.MPESA_QUEUE.send(body)

        # Acknowledge the callback immediately.
        return Response("OK", status=200)
