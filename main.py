from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel
import httpx
import os
from dotenv import load_dotenv
from psycopg_pool import AsyncConnectionPool

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

pool: AsyncConnectionPool

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False)
    await pool.open()
    yield
    await pool.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "https://jshmslf.is-a.dev"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

ABACUS_BASE = "https://abacus.jasoncameron.dev"
ABACUS_KEY  = "jshmslf.is-a.dev/portfolio-likes"

class LikePayload(BaseModel):
    token: str
    count: int  # batch count from debounce

@app.get("/api/like")
async def get_like(token: str):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count FROM likes_visitors WHERE token = %s", (token,)
            )
            row = await cur.fetchone()
    visitor_likes = row[0] if row else 0

    async with httpx.AsyncClient() as client:
        res = await client.get(f"{ABACUS_BASE}/get/{ABACUS_KEY}")
    total_likes = res.json().get("value", 0)

    return {"total_likes": total_likes, "visitor_likes": visitor_likes}

@app.post("/api/like")
async def post_like(payload: LikePayload):
    token = payload.token
    count = max(1, min(payload.count, 100))  # clamp between 1–100 to prevent abuse

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
                (token, count),
            )
            row = await cur.fetchone()
    visitor_likes = row[0]

    # Hit Abacus `count` times to match the batch
    total_likes = 0
    async with httpx.AsyncClient() as client:
        for _ in range(count):
            res = await client.get(f"{ABACUS_BASE}/hit/{ABACUS_KEY}")
        total_likes = res.json().get("value", 0)

    return {"total_likes": total_likes, "visitor_likes": visitor_likes}