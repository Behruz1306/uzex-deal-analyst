import json
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import agent  # noqa: E402
import tgbot  # noqa: E402

app = FastAPI(title="UZEX Deal Analyst")
STATIC = Path(__file__).with_name("static")
RATE = int(os.environ.get("RATE_PER_10MIN", "300"))
_hits = defaultdict(deque)
_lock = threading.Lock()


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[dict] = []
    lang: str = "ru"


def _limited(ip: str) -> bool:
    now = time.time()
    with _lock:
        q = _hits[ip]
        while q and now - q[0] > 600:
            q.popleft()
        if len(q) >= RATE:
            return True
        q.append(now)
    return False


@app.on_event("startup")
def warm():
    agent._build_index()
    if os.environ.get("RUN_TGBOT", "1") == "1":
        tgbot.start_background()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "model": agent.MODEL}


@app.get("/api/stats")
def stats():
    r = agent.db().execute("""select count(*), min(deal_date), max(deal_date),
        (select count(*) from contracts), (select count(*) from participants), (select count(*) from brokers)
        from deals""").fetchone()
    return {"deals": r[0], "from": r[1].isoformat(), "to": r[2].isoformat(), "contracts": r[3],
            "participants": r[4], "brokers": r[5]}


_tape = None


@app.get("/api/tape")
def tape():
    global _tape
    if _tape is None:
        rows = agent.db().execute("""
            select product_family, product_family_uz, brand, region, unit, currency,
                   round(sum(amount)/sum(volume), 2) price, count(*) n
            from v_deals where deal_date = (select max(deal_date) from deals) and is_valid
            group by all order by n desc limit 24""").fetchall()
        _tape = [dict(zip(["family", "family_uz", "brand", "region", "unit", "currency", "price", "n"], r)) for r in rows]
    return _tape


@app.post("/api/ask")
def ask(body: Ask, request: Request):
    ip = request.headers.get("fly-client-ip") or (request.client.host if request.client else "?")
    if _limited(ip):
        raise HTTPException(429, "Too many requests, try again in a few minutes")

    def gen():
        try:
            for ev in agent.run_agent(body.question, body.history, body.lang[:8]):
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)[:300]})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
