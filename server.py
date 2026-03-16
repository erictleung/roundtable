#!/usr/bin/env python3
"""
ContactSwap — Redis-backed FastAPI app.

Environment variables
---------------------
  REDIS_URL  — connection string, e.g. redis://default:password@host:6379
               Set this in Leapcell -> Service -> Environment.

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


# ── Key / channel helpers ─────────────────────────────────────────────────────
def _new_id(k: int = 12) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=k))

def _room_key(code: str) -> str:
    return f"room:{code}"

def _member_key(code: str, member_id: str) -> str:
    return f"room:{code}:member:{member_id}"

def _channel(code: str) -> str:
    return f"room:{code}"


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
    connected = meta.get("connected") == "1"
    members   = await _get_members(r, code, meta)
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


# ── URL sanitisation helper ──────────────────────────────────────────────────
def _sanitise_redis_url(raw: str) -> str:
    """
    1. Strip surrounding whitespace and accidental quote characters.
    2. If the password contains URL-unsafe characters (e.g. + / =) that were
       pasted in un-encoded, percent-encode just the password so the URL
       parser can correctly identify the port.
    3. Validate that scheme, host, and port are all present.
    """
    from urllib.parse import quote

    url = raw.strip().strip(chr(39)).strip(chr(34))

    # urlparse raises ValueError when special chars in the password (e.g. +)
    # make the port look non-numeric. Catch it and re-encode the password.
    needs_encode = False
    try:
        parsed = urlparse(url)
        _ = parsed.port  # accessing .port triggers the ValueError
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

    # Leapcell injects HOST, PORT, and PASSWORD as separate environment
    # variables for linked Redis instances. If all three are present, build
    # the connection directly from those. Fall back to a REDIS_URL string
    # (useful for local dev with a single connection string).
    host     = os.environ.get("HOST")
    port_str = os.environ.get("PORT")
    password = os.environ.get("PASSWORD")

    if host and port_str and password:
        try:
            port = int(port_str)
        except ValueError:
            raise RuntimeError("PORT env var is not a valid integer: " + repr(port_str))
        print("Connecting to Redis via HOST/PORT/PASSWORD — host=" + host + " port=" + str(port))
        _redis = aioredis.Redis(
            host=host,
            port=port,
            password=password,
            username="default",
            decode_responses=False,
            ssl=True,
        )
    else:
        # Fall back to a full REDIS_URL string (local dev or manual config)
        raw_url   = os.environ.get("REDIS_URL", "redis://localhost:6379")
        redis_url = _sanitise_redis_url(raw_url)
        parsed    = urlparse(redis_url)
        print("Connecting to Redis via REDIS_URL — host=" + str(parsed.hostname) + " port=" + str(parsed.port))
        _redis = aioredis.from_url(redis_url, decode_responses=False)

    try:
        await _redis.ping()
    except Exception as e:
        raise RuntimeError("Redis ping failed: " + str(e))

    print("Redis connection established.")
    yield
    await _redis.aclose()


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="ContactSwap", lifespan=lifespan)


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
    r = get_redis()
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

    await r.publish(_channel(req.code), "join")

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
    await r.publish(_channel(req.code), "connected")

    return {"ok": True}


@app.get("/api/poll")
async def poll(code: str, member_id: str, count: int = 0):
    r    = get_redis()
    meta = await _get_room_meta(r, code)

    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")

    member_ids = json.loads(meta.get("member_ids", "[]"))

    # Retry the membership check briefly to handle the race condition where
    # the browser starts polling immediately after join_room returns, but
    # the Redis write hasn't propagated to this read yet.
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

    # Return immediately if state already changed
    if len(member_ids) != count or meta.get("connected") == "1":
        return await _room_state(r, code)

    # Block on pub/sub until a change event or timeout
    pubsub = r.pubsub()
    await pubsub.subscribe(_channel(code))
    try:
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining),
                    timeout=remaining + 0.1,
                )
            except asyncio.TimeoutError:
                break
            if msg is not None:
                break
            await asyncio.sleep(0.05)
    finally:
        await pubsub.unsubscribe(_channel(code))
        await pubsub.aclose()

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
            await r.publish(_channel(req.code), "leave")
    return {"ok": True}


# -- Debug endpoint ------------------------------------------------------
# Shows Redis connectivity status and live room keys.
# Remove or restrict access before going public.
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


# ── Serve frontend ────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "index.html")


# ── Dev entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)
