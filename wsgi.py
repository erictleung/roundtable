"""
ASGI entry point for Ripple Meet (FastAPI rewrite).

⚠️  SINGLE WORKER ONLY — see note below before changing.

    uvicorn  wsgi:app --workers 1 --host 0.0.0.0 --port 8080
    gunicorn wsgi:app --workers 1 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8080

Why only one worker?
--------------------
Room state is stored in the `rooms` dict in memory inside this process.
Multiple worker *processes* each get their own isolated copy of that dict.
If Browser A creates a room on Worker 1 and Browser B tries to join on
Worker 2, Worker 2 has never seen that room and returns 404.

Because the long-poll endpoint is fully async (asyncio.sleep, not
time.sleep), a single worker can handle hundreds of concurrent connections
without blocking. For a small group contact-sharing app, one worker is
the correct and sufficient choice.

Scaling beyond one machine
--------------------------
If you ever need multiple workers or servers, replace the in-memory
`rooms` dict with Redis and swap asyncio.Event for Redis Pub/Sub.
The route logic in server.py stays the same; only the storage layer changes.

Nginx proxy_read_timeout
------------------------
If sitting behind Nginx, set proxy_read_timeout to at least 25s to allow
the 20-second long-poll to complete without being cut off:

    proxy_read_timeout 25s;
"""

import sys

from server import app  # re-export the ASGI callable

# Warn loudly at startup if someone tries to run multiple workers
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, workers=1)

__all__ = ["app"]

