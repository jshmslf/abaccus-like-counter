from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel
import asyncio
import json
import os
from dotenv import load_dotenv
from psycopg_pool import AsyncConnectionPool

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

pool: AsyncConnectionPool
_sse_queues: list[asyncio.Queue] = []


async def get_total(conn) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT COALESCE(SUM(count), 0) FROM likes_visitors")
        row = await cur.fetchone()
    return int(row[0])


async def pg_listener():
    """Single persistent connection that listens for Postgres NOTIFY."""
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        await conn.execute("LISTEN likes_updated")

        async for notify in conn.notifies():
            # On any notify, fetch fresh total and fan-out to all SSE clients
            async with pool.connection() as fetch_conn:
                total = await get_total(fetch_conn)

            dead = []
            for q in _sse_queues:
                try:
                    q.put_nowait(total)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                _sse_queues.remove(q)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False)
    await pool.open()
    asyncio.create_task(pg_listener())
    yield
    await pool.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "https://jshmslf.is-a.dev"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class LikePayload(BaseModel):
    token: str
    count: int


@app.get("/api/like")
async def get_like(token: str):
    async with pool.connection() as conn:
        total = await get_total(conn)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count FROM likes_visitors WHERE token = %s", (token,)
            )
            row = await cur.fetchone()
    visitor_likes = row[0] if row else 0
    return {"total_likes": total, "visitor_likes": visitor_likes}


@app.post("/api/like")
async def post_like(payload: LikePayload):
    count = max(1, min(payload.count, 500))

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO likes_visitors (token, count)
                VALUES (%s, %s)
                ON CONFLICT (token) DO UPDATE
                  SET count = likes_visitors.count + EXCLUDED.count,
                      updated_at = NOW()
                RETURNING count
                """,
                (payload.token, count),
            )
            row = await cur.fetchone()
    # Trigger fires automatically → pg_listener fans out to SSE clients
    return {"visitor_likes": row[0]}


@app.get("/api/like/stream")
async def like_stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    _sse_queues.append(queue)

    # Push current total immediately on connect
    async with pool.connection() as conn:
        total = await get_total(conn)
    await queue.put(total)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    total = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {json.dumps({'total': total})}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # prevents proxy/nginx timeout
        finally:
            if queue in _sse_queues:
                _sse_queues.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )