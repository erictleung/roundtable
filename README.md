# Ripple Meet

Share contact links with a group instantly — no accounts, no app install, no pairwise sending.

## The name

When you drop a stone into still water, a single point of contact sends rings outward in every direction simultaneously — each person reached at the same moment, equally, without hierarchy.

That's exactly what Ripple Meet does. One person in a room triggers the exchange, and everyone's contact details spread outward to the whole group at once. No pairwise back-and-forth, no one left waiting, no centre of power. Just a single moment that reaches everyone at the same time.

The name was chosen deliberately over alternatives like *RoundTable* (which can imply a fixed seat and exclusivity) in favour of something that feels more open, spontaneous, and human — like the best in-person gatherings themselves.

## How it works

1. **One person** goes to the site, enters their name + contact URL, and clicks **"Create room"** → gets a 4-digit code.
2. **Everyone else** goes to the same site, enters their info, clicks **"Join room"**, and types the code.
3. When everyone is in, **one person** clicks **"⚡ Connect Everyone"** → the full list of contact URLs appears for everyone at once.

## Files

| File | Purpose |
|------|---------|
| `server.py` | FastAPI application — routes, models, async long-poll logic |
| `wsgi.py` | ASGI entry point — re-exports `app` for production servers |
| `index.html` | Single-page frontend (served by FastAPI) |
| `requirements.txt` | Python dependencies (`fastapi`, `uvicorn`) |

## Setup

```bash
pip install -r requirements.txt
```

## Development

```bash
uvicorn server:app --reload --port 8080
# → http://localhost:8080
# --reload restarts the server automatically when you edit server.py
```

## Production (ASGI)

FastAPI is an **ASGI** framework (not WSGI). Use an ASGI-compatible server.

### Uvicorn (simplest)

```bash
uvicorn wsgi:app --host 0.0.0.0 --port 8080 --workers 1
```

### Gunicorn + Uvicorn workers (recommended for production)

```bash
pip install gunicorn
gunicorn wsgi:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers 1 \
  --bind 0.0.0.0:8080
```

> ⚠️ **Always use `--workers 1`.** Room state lives in memory inside the
> process. Multiple worker processes each get their own isolated copy of
> `rooms`, so a user who creates a room on Worker 1 will get a 404 when
> a friend tries to join on Worker 2. Because the long-poll is fully async,
> one worker handles hundreds of concurrent users without blocking.

### Nginx (reverse proxy)

```nginx
location / {
    proxy_pass         http://127.0.0.1:8080;
    proxy_read_timeout 25s;   # must exceed the 20s long-poll timeout
}
```

## Performance improvements over v1

| Issue | v1 (http.server) | v2 (FastAPI) |
|---|---|---|
| Long-poll blocks a thread | `time.sleep(1)` loop holds thread for up to 20s | `await asyncio.sleep()` suspends coroutine, thread is free |
| Poll wake-up latency | Up to 1s delay when someone joins | Instant — `asyncio.Event.set()` wakes waiting polls immediately |
| Lock contention | Single global lock for all rooms | Per-room `asyncio.Lock` — rooms never block each other |
| Request validation | Manual `str(body.get(...))` checks | Pydantic models with automatic type coercion and error messages |
| Cleanup | Called on every poll request | Dedicated background task running every 60s |

## Automatic API docs

FastAPI generates interactive API docs automatically:

- **Swagger UI**: http://localhost:8080/docs
- **ReDoc**: http://localhost:8080/redoc

## Multi-server note

Room state lives in memory, so members must hit the same server process.
For multi-server deployments, swap the `rooms` dict for Redis and replace
`asyncio.Event` with Redis Pub/Sub — the rest of the code stays the same.
