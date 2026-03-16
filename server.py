#!/usr/bin/env python3
"""
Ripple Meet — Redis-backed FastAPI app.

Signalling strategy
-------------------
Leapcell Redis does not support SUBSCRIBE/PUBLISH (pub/sub).
Instead we use a Redis List as a signal queue per room:
  - On join/connect/leave: LPUSH room:{code}:signal "event"
  - In poll:               LPOP room:{code}:signal timeout=20
LPOP blocks until an item appears (instant wake-up) or the
timeout expires, using only standard List commands.

Environment variables
---------------------
  HOST, PORT, PASSWORD  — injected by Leapcell when Redis is linked
  REDIS_URL             — fallback full connection string for local dev
  REDIS_SSL             — set to "false" to disable TLS (local dev only)

Dev server:   uvicorn server:app --reload --port 8080
Production:   uvicorn wsgi:app --port 8080 --workers 1
"""

import asyncio
import json
import os
import random
import string
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl, field_validator

# ── Constants ─────────────────────────────────────────────────────────────────
ROOM_TTL     = 3600   # Redis key TTL in seconds (1 hour)
POLL_TIMEOUT = 20     # long-poll max wait in seconds
BASE_DIR     = Path(__file__).parent

# ── Redis client (set during lifespan startup) ────────────────────────────────
_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialised")
    return _redis


# ── Key helpers ───────────────────────────────────────────────────────────────
def _new_id(k: int = 12) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=k))

def _room_key(code: str) -> str:
    return f"room:{code}"

def _member_key(code: str, member_id: str) -> str:
    return f"room:{code}:member:{member_id}"

def _signal_key(code: str) -> str:
    """List key used to wake up waiting polls via LPOP polling."""
    return f"room:{code}:signal"


# ── Redis data helpers ────────────────────────────────────────────────────────
async def _get_room_meta(r: aioredis.Redis, code: str) -> dict | None:
    data = await r.hgetall(_room_key(code))
    if not data:
        return None
    return {k.decode(): v.decode() for k, v in data.items()}


async def _get_members(r: aioredis.Redis, code: str, meta: dict) -> list[dict]:
    member_ids = json.loads(meta.get("member_ids", "[]"))
    members = []
    for mid in member_ids:
        raw = await r.hgetall(_member_key(code, mid))
        if raw:
            members.append({k.decode(): v.decode() for k, v in raw.items()})
    return members


async def _room_state(r: aioredis.Redis, code: str) -> dict:
    meta = await _get_room_meta(r, code)
    if not meta:
        return {}
    connected   = meta.get("connected") == "1"
    members     = await _get_members(r, code, meta)
    members_out = (
        members if connected
        else [{"id": m["id"], "name": m["name"]} for m in members]
    )
    return {
        "connected":    connected,
        "member_count": len(members),
        "members":      members_out,
    }


async def _new_room_code(r: aioredis.Redis) -> str:
    for _ in range(200):
        code = "".join(random.choices(string.digits, k=4))
        if not await r.exists(_room_key(code)):
            return code
    raise RuntimeError("Could not generate a unique room code")


async def _touch_room(r: aioredis.Redis, code: str):
    await r.hset(_room_key(code), "last_activity", str(time.time()))
    await r.expire(_room_key(code), ROOM_TTL)


async def _signal(r: aioredis.Redis, code: str, event: str):
    """
    Wake up all polls waiting on this room by pushing to the signal list.
    Each waiting LPOP loop consumes one item when it wakes up, so we
    push one item per expected waiter. A fixed number (10) is safe —
    unconsumed items expire with the signal key TTL.
    """
    key = _signal_key(code)
    await r.lpush(key, *([event] * 10))
    await r.expire(key, 60)   # short TTL — signals are ephemeral


# ── URL sanitisation helper ───────────────────────────────────────────────────
def _sanitise_redis_url(raw: str) -> str:
    from urllib.parse import quote
    url = raw.strip().strip(chr(39)).strip(chr(34))
    needs_encode = False
    try:
        parsed = urlparse(url)
        _ = parsed.port
        if parsed.port is None and chr(64) in url:
            needs_encode = True
    except ValueError:
        needs_encode = True

    if needs_encode and chr(64) in url:
        scheme_and_creds, hostpart = url.rsplit(chr(64), 1)
        if "://" in scheme_and_creds:
            scheme, creds = scheme_and_creds.split("://", 1)
        else:
            scheme, creds = "redis", scheme_and_creds
        if ":" in creds:
            user, password = creds.split(":", 1)
            url = scheme + "://" + user + ":" + quote(password, safe="") + chr(64) + hostpart

    parsed = urlparse(url)
    errors = []
    if parsed.scheme not in ("redis", "rediss"):
        errors.append("scheme must be redis or rediss, got: " + repr(parsed.scheme))
    if not parsed.hostname:
        errors.append("no hostname found")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        errors.append("no valid port found (expected e.g. :6379)")
    if errors:
        raise RuntimeError(
            "REDIS_URL is invalid. Raw=" + repr(raw) +
            " Processed=" + repr(url) +
            " Problems: " + "; ".join(errors) +
            " Tip: encode + as %2B, / as %2F, = as %3D"
        )
    return url


# ── App lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis

    host     = os.environ.get("HOST")
    port_str = os.environ.get("PORT")
    password = os.environ.get("PASSWORD")

    if host and port_str and password:
        try:
            port = int(port_str)
        except ValueError:
            raise RuntimeError("PORT env var is not a valid integer: " + repr(port_str))

        use_ssl = os.environ.get("REDIS_SSL", "true").lower() != "false"
        print("[Redis] host=" + host + " port=" + str(port) + " ssl=" + str(use_ssl))

        _redis = aioredis.Redis(
            host=host,
            port=port,
            password=password,
            username="default",
            decode_responses=False,
            ssl=use_ssl,
            ssl_cert_reqs="none",
            socket_keepalive=True,
            health_check_interval=30,
        )
    else:
        raw_url   = os.environ.get("REDIS_URL", "redis://localhost:6379")
        redis_url = _sanitise_redis_url(raw_url)
        parsed    = urlparse(redis_url)
        print("[Redis] connecting via REDIS_URL host=" + str(parsed.hostname) + " port=" + str(parsed.port))
        _redis = aioredis.from_url(redis_url, decode_responses=False)

    try:
        await _redis.ping()
    except Exception as e:
        raise RuntimeError("Redis ping failed: " + str(e))

    print("[Redis] connection established.")
    yield
    await _redis.aclose()


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Ripple Meet", lifespan=lifespan)


# ── Request schemas ───────────────────────────────────────────────────────────
class CreateRoomRequest(BaseModel):
    name: str
    url: HttpUrl

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()[:60]
        if not v:
            raise ValueError("name must not be empty")
        return v


class JoinRoomRequest(BaseModel):
    code: str
    name: str
    url: HttpUrl

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()[:60]
        if not v:
            raise ValueError("name must not be empty")
        return v

    @field_validator("code")
    @classmethod
    def code_is_digits(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or len(v) != 4:
            raise ValueError("code must be exactly 4 digits")
        return v


class RoomMemberRequest(BaseModel):
    code: str
    member_id: str


# ── API routes ────────────────────────────────────────────────────────────────
@app.post("/api/create_room")
async def create_room(req: CreateRoomRequest):
    r         = get_redis()
    code      = await _new_room_code(r)
    member_id = _new_id()
    now       = time.time()

    await r.hset(_room_key(code), mapping={
        "code":          code,
        "created_at":    str(now),
        "last_activity": str(now),
        "connected":     "0",
        "member_ids":    json.dumps([member_id]),
    })
    await r.expire(_room_key(code), ROOM_TTL)

    await r.hset(_member_key(code, member_id), mapping={
        "id":   member_id,
        "name": req.name,
        "url":  str(req.url),
    })
    await r.expire(_member_key(code, member_id), ROOM_TTL)

    return {"room_code": code, "member_id": member_id}


@app.post("/api/join_room")
async def join_room(req: JoinRoomRequest):
    r    = get_redis()
    meta = await _get_room_meta(r, req.code)

    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Room not found. Check the code and try again.")
    if meta.get("connected") == "1":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This room has already been connected. "
                            "Ask your group to start a new room.")

    member_id  = _new_id()
    member_ids = json.loads(meta.get("member_ids", "[]"))
    member_ids.append(member_id)

    await r.hset(_room_key(req.code), "member_ids", json.dumps(member_ids))
    await _touch_room(r, req.code)

    await r.hset(_member_key(req.code, member_id), mapping={
        "id":   member_id,
        "name": req.name,
        "url":  str(req.url),
    })
    await r.expire(_member_key(req.code, member_id), ROOM_TTL)

    # Wake up waiting polls
    await _signal(r, req.code, "join")

    return {"room_code": req.code, "member_id": member_id}


@app.post("/api/connect")
async def connect(req: RoomMemberRequest):
    r    = get_redis()
    meta = await _get_room_meta(r, req.code)

    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")

    member_ids = json.loads(meta.get("member_ids", "[]"))
    if req.member_id not in member_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this room")

    await r.hset(_room_key(req.code), "connected", "1")
    await _touch_room(r, req.code)

    # Wake up all waiting polls
    await _signal(r, req.code, "connected")

    return {"ok": True}


@app.get("/api/poll")
async def poll(code: str, member_id: str, count: int = 0):
    """
    Async long-poll using LPOP polling.

    Loops with LPOP + asyncio.sleep(0.5) until a signal item appears
    or the timeout expires. Replaces pub/sub and LPOP, neither of
    which are supported by Leapcell Redis.
    """
    r    = get_redis()
    meta = await _get_room_meta(r, code)

    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")

    member_ids = json.loads(meta.get("member_ids", "[]"))

    # Brief retry to handle the race where the browser polls immediately
    # after join_room but the Redis write hasn't been read back yet.
    if member_id not in member_ids:
        for _ in range(5):
            await asyncio.sleep(0.2)
            meta = await _get_room_meta(r, code)
            if meta is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")
            member_ids = json.loads(meta.get("member_ids", "[]"))
            if member_id in member_ids:
                break
        else:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member")

    # Return immediately if state already changed since last poll
    if len(member_ids) != count or meta.get("connected") == "1":
        return await _room_state(r, code)

    # Poll the signal list with LPOP until an item appears or we time out.
    # LPOP is not supported by Leapcell Redis, so we use a plain LPOP
    # loop with asyncio.sleep() — the await yields the event loop between
    # checks so no threads are blocked.
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        item = await r.lpop(_signal_key(code))
        if item is not None:
            break
        await asyncio.sleep(0.5)

    return await _room_state(r, code)


@app.post("/api/leave_room")
async def leave_room(req: RoomMemberRequest):
    r    = get_redis()
    meta = await _get_room_meta(r, req.code)
    if meta:
        member_ids = json.loads(meta.get("member_ids", "[]"))
        if req.member_id in member_ids:
            member_ids.remove(req.member_id)
            await r.hset(_room_key(req.code), "member_ids", json.dumps(member_ids))
            await r.delete(_member_key(req.code, req.member_id))
            await _touch_room(r, req.code)
            await _signal(r, req.code, "leave")
    return {"ok": True}


# ── Debug endpoint ────────────────────────────────────────────────────────────
@app.get("/api/debug")
async def debug():
    host     = os.environ.get("HOST")
    port_str = os.environ.get("PORT")
    password = os.environ.get("PASSWORD")

    if host and port_str and password:
        conn_method = "HOST/PORT/PASSWORD env vars"
        redis_host  = host
        redis_port  = port_str
    else:
        raw_url     = os.environ.get("REDIS_URL", "(not set)")
        parsed      = urlparse(raw_url.strip())
        conn_method = "REDIS_URL env var"
        redis_host  = str(parsed.hostname)
        redis_port  = str(parsed.port)

    r          = get_redis()
    ping_ok    = False
    ping_error = None
    try:
        await r.ping()
        ping_ok = True
    except Exception as e:
        ping_error = str(e)

    keys = [k.decode() async for k in r.scan_iter("room:????")] if ping_ok else []

    return {
        "worker_pid":  os.getpid(),
        "conn_method": conn_method,
        "redis_host":  redis_host,
        "redis_port":  redis_port,
        "ping_ok":     ping_ok,
        "ping_error":  ping_error,
        "room_keys":   keys,
        "room_count":  len(keys),
    }


# ── Leapcell health check ─────────────────────────────────────────────────────
@app.get("/kaithhealth")
@app.get("/kaithheathcheck")
async def health_check():
    return {"status": "ok"}


# ── Serve frontend ────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "index.html")


# ── Dev entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)
