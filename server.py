#!/usr/bin/env python3
"""
RoundTable — FastAPI rewrite.

Performance improvements over the original http.server version:
  1. Async long-poll: threads are no longer pinned during the 20-second wait.
     asyncio.sleep() yields the event loop so other requests run freely.
  2. asyncio.Event per room: polls wake up *instantly* when state changes
     instead of sleeping up to 1 second between checks.
  3. Per-room locks: requests for different rooms never block each other.
  4. Pydantic models: request bodies are validated and typed automatically.
  5. FastAPI's StaticFiles: index.html is served efficiently without custom code.

Dev server:       uvicorn server:app --reload --port 8080
Production:       see wsgi.py / README
"""

import asyncio
import random
import string
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl, field_validator

# ── Constants ─────────────────────────────────────────────────────────────────
ROOM_TTL = 3600        # seconds before an idle room is evicted
POLL_TIMEOUT = 20      # seconds a long-poll request waits for a change
BASE_DIR = Path(__file__).parent


# ── Data models ───────────────────────────────────────────────────────────────
class Member(BaseModel):
    id: str
    name: str
    url: str


class Room(BaseModel):
    code: str
    created_at: float
    last_activity: float
    connected: bool = False
    members: dict[str, Member] = {}


# ── In-memory store ───────────────────────────────────────────────────────────
# Each room gets its own asyncio.Event so polls wake up the instant
# someone joins or the room is connected, with zero polling delay.
rooms: dict[str, Room] = {}
room_locks: dict[str, asyncio.Lock] = {}      # per-room lock — rooms don't block each other
room_events: dict[str, asyncio.Event] = {}    # per-room wake-up signal


def _new_id(k: int = 12) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=k))


def _new_room_code() -> str:
    for _ in range(200):
        code = "".join(random.choices(string.digits, k=4))
        if code not in rooms:
            return code
    raise RuntimeError("Could not generate a unique room code")


def _room_state(room: Room, member_id: str) -> dict:
    """Return the payload sent to polling clients."""
    members_out = (
        [m.model_dump() for m in room.members.values()]
        if room.connected
        else [{"id": m.id, "name": m.name} for m in room.members.values()]
    )
    return {
        "connected": room.connected,
        "member_count": len(room.members),
        "members": members_out,
    }


# ── Background cleanup task ───────────────────────────────────────────────────
async def _cleanup_loop():
    """Evict rooms that have been idle for more than ROOM_TTL seconds."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        expired = [
            code for code, room in list(rooms.items())
            if now - room.last_activity > ROOM_TTL
        ]
        for code in expired:
            rooms.pop(code, None)
            room_locks.pop(code, None)
            room_events.pop(code, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="ContactSwap", lifespan=lifespan)


# ── Request / response schemas ────────────────────────────────────────────────
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
    code = _new_room_code()
    member_id = _new_id()
    now = time.time()
    url_str = str(req.url)

    room = Room(
        code=code,
        created_at=now,
        last_activity=now,
        members={member_id: Member(id=member_id, name=req.name, url=url_str)},
    )
    rooms[code] = room
    room_locks[code] = asyncio.Lock()
    room_events[code] = asyncio.Event()

    return {"room_code": code, "member_id": member_id}


@app.post("/api/join_room")
async def join_room(req: JoinRoomRequest):
    room = rooms.get(req.code)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Room not found. Check the code and try again.")
    if room.connected:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This room has already been connected. "
                            "Ask your group to start a new room.")

    member_id = _new_id()
    url_str = str(req.url)

    async with room_locks[req.code]:
        room.members[member_id] = Member(id=member_id, name=req.name, url=url_str)
        room.last_activity = time.time()

    # Signal all waiting polls for this room immediately
    room_events[req.code].set()
    room_events[req.code].clear()

    return {"room_code": req.code, "member_id": member_id}


@app.post("/api/connect")
async def connect(req: RoomMemberRequest):
    room = rooms.get(req.code)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")
    if req.member_id not in room.members:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this room")

    async with room_locks[req.code]:
        room.connected = True
        room.last_activity = time.time()

    # Wake up every waiting poll at once — they'll all see connected=True
    room_events[req.code].set()

    return {"ok": True}


@app.get("/api/poll")
async def poll(code: str, member_id: str, count: int = 0):
    """
    Async long-poll endpoint.

    The coroutine suspends with `await asyncio.wait_for(event.wait(), ...)`
    instead of blocking a thread. The event loop is free to serve other
    requests while this one waits for a state change.
    """
    room = rooms.get(code)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")
    if member_id not in room.members:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member")

    event = room_events.get(code)
    deadline = time.time() + POLL_TIMEOUT

    while time.time() < deadline:
        current_count = len(room.members)

        # Return immediately if something changed since the last poll
        if current_count != count or room.connected:
            return _room_state(room, member_id)

        # Suspend this coroutine until the event fires or we time out.
        # Unlike time.sleep(), this releases the event loop to handle
        # other requests concurrently.
        remaining = deadline - time.time()
        try:
            await asyncio.wait_for(asyncio.shield(event.wait()), timeout=remaining)
        except asyncio.TimeoutError:
            break

    # Timeout — return current state; client will re-poll
    return _room_state(room, member_id)


@app.post("/api/leave_room")
async def leave_room(req: RoomMemberRequest):
    room = rooms.get(req.code)
    if room and req.member_id in room.members:
        async with room_locks[req.code]:
            room.members.pop(req.member_id, None)
            room.last_activity = time.time()
        room_events[req.code].set()
        room_events[req.code].clear()
    return {"ok": True}


@app.get('/api/debug')
async def debug():
    return {
        "worker_pid": os.getpid(),
        "room_codes": list(rooms.keys()),
        "room_count": len(rooms),
    }


# ── Static files ──────────────────────────────────────────────────────────────
# Serve index.html at / — FastAPI's StaticFiles handles this efficiently.
@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "index.html")


# ── Dev entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)
