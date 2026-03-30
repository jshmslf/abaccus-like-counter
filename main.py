from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx
import os
from dotenv import load_dotenv
from psycopg_pool import AsyncConnectionPool

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]  # Neon connection string

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

def get_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host or "unknown"

@app.get("/api/like")
async def get_like(request: Request):
    ip = get_ip(request)

    async with pool.connection() as conn:
        row = await conn.execute(
            "SELECT count FROM likes_visitors WHERE ip = %s", (ip,)
        )
        row = await row.fetchone()
    visitor_likes = row[0] if row else 0

    async with httpx.AsyncClient() as client:
        res = await client.get(
            "https://abacus.jasoncameron.dev/get/jshmslf.is-a.dev/portfolio-likes"
        )
    total_likes = res.json().get("value", 0)

    return {"total_likes": total_likes, "visitor_likes": visitor_likes}

@app.post("/api/like")
async def post_like(request: Request):
    ip = get_ip(request)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO likes_visitors (ip, count)
                VALUES (%s, 1)
                ON CONFLICT (ip) DO UPDATE
                SET count = likes_visitors.count + 1,
                    updated_at = NOW()
                RETURNING count
                """,
                (ip,),
            )
            row = await cur.fetchone()
    visitor_likes = row[0]

    async with httpx.AsyncClient() as client:
        res = await client.get(
            "https://abacus.jasoncameron.dev/hit/jshmslf.is-a.dev/portfolio-likes"
        )
    total_likes = res.json().get("value", 0)

    return {"total_likes": total_likes, "visitor_likes": visitor_likes}