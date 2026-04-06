"""
OMEGA Backend v2.0
- /claude     → proxy al API de Anthropic (predicciones IA)
- /trade      → ejecuta órdenes en Polymarket CLOB
- /status     → estado del bot y balance
- /history    → historial de trades ejecutados
"""

import os, time, json, asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
POLY_PRIVATE_KEY  = os.getenv("POLY_PRIVATE_KEY", "")
POLY_API_KEY      = os.getenv("POLY_API_KEY", "")
POLY_SECRET       = os.getenv("POLY_SECRET", "")
POLY_PASSPHRASE   = os.getenv("POLY_PASSPHRASE", "")
POLY_WALLET       = os.getenv("POLY_WALLET", "")

CLOB_BASE    = "https://clob.polymarket.com"
GAMMA_BASE   = "https://gamma-api.polymarket.com"
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

MAX_BET_USD    = float(os.getenv("MAX_BET_USD", "5.0"))
MIN_BET_USD    = float(os.getenv("MIN_BET_USD", "1.0"))
MIN_CONFIDENCE = int(os.getenv("MIN_CONFIDENCE", "65"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))
BAD_HOURS      = [int(h) for h in os.getenv("BAD_HOURS", "4,0,16,23,22,8,9").split(",")]

app = FastAPI(title="OMEGA Backend", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

trade_log: list[dict] = []
session_stats = {"trades": 0, "wins": 0, "losses": 0, "pending": 0, "pnl": 0.0}

_poly_client = None

def get_poly_client():
    global _poly_client
    if _poly_client is not None:
        return _poly_client
    if not POLY_PRIVATE_KEY:
        return None
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        host = CLOB_BASE
        chain_id = 137

        if POLY_API_KEY and POLY_SECRET and POLY_PASSPHRASE:
            creds = ApiCreds(
                api_key=POLY_API_KEY,
                api_secret=POLY_SECRET,
                api_passphrase=POLY_PASSPHRASE,
            )
            _poly_client = ClobClient(host=host, chain_id=chain_id, key=POLY_PRIVATE_KEY, creds=creds, signature_type=0)
        else:
            client_l1 = ClobClient(host=host, chain_id=chain_id, key=POLY_PRIVATE_KEY)
            creds = client_l1.create_or_derive_api_creds()
            _poly_client = ClobClient(host=host, chain_id=chain_id, key=POLY_PRIVATE_KEY, creds=creds, signature_type=0)
            print(f"[POLY] Creds derivadas — API Key: {creds.api_key}")

        print("[POLY] Cliente inicializado ✅")
        return _poly_client
    except Exception as e:
        print(f"[POLY] Error: {e}")
        return None

class ClaudeRequest(BaseModel):
    prompt: str

class TradeRequest(BaseModel):
    direction: str
    confidence: int
    token_up: Optional[str] = None
    token_down: Optional[str] = None
    bankroll: Optional[float] = None
    signal_quality: Optional[str] = "MODERADA"
    hour: Optional[int] = None

def kelly_bet(confidence: int, bankroll: float, signal_quality: str) -> float:
    p = confidence / 100.0
    kelly_f = max(0, (p * 2 - 1))
    bet = bankroll * kelly_f * KELLY_FRACTION
    sq_mult = {"FUERTE": 1.0, "MODERADA": 0.7, "DÉBIL": 0.4}.get(signal_quality, 0.5)
    return round(max(MIN_BET_USD, min(MAX_BET_USD, bet * sq_mult)), 2)

def is_bad_hour(hour: Optional[int]) -> bool:
    h = hour if hour is not None else datetime.now(timezone.utc).hour
    return h in BAD_HOURS

async def get_token_id(direction: str, cur_slot: int) -> Optional[str]:
    async with httpx.AsyncClient(timeout=10) as client:
        for s in [f"btc-updown-5m-{cur_slot}", f"btc-updown-5m-{cur_slot-300}", f"btc-updown-5m-{cur_slot+300}"]:
            try:
                r = await client.get(f"{GAMMA_BASE}/events?slug={s}")
                if r.status_code != 200: continue
                events = r.json()
                if not events: continue
                market = events[0].get("markets", [{}])[0]
                tokens = json.loads(market.get("clobTokenIds", "[]"))
                if len(tokens) >= 2:
                    return tokens[0] if direction == "UP" else tokens[1]
            except Exception:
                continue
    return None

@app.post("/claude")
async def claude_proxy(req: ClaudeRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY no configurada")
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": req.prompt}],
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=payload,
        )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    data = r.json()
    text = data["content"][0]["text"] if data.get("content") else ""
    return {"text": text}

@app.post("/trade")
async def execute_trade(req: TradeRequest):
    now_hour = req.hour if req.hour is not None else datetime.now(timezone.utc).hour
    cur_slot = (int(time.time()) // 300) * 300

    if req.direction == "NEUTRAL":
        return {"executed": False, "reason": "señal NEUTRAL"}
    if req.confidence < MIN_CONFIDENCE:
        return {"executed": False, "reason": f"confianza {req.confidence}% < mínimo {MIN_CONFIDENCE}%"}
    if is_bad_hour(now_hour):
        return {"executed": False, "reason": f"hora {now_hour}h — WR histórico bajo"}
    if req.signal_quality == "DÉBIL":
        return {"executed": False, "reason": "señal DÉBIL"}

    bankroll = req.bankroll or 50.0
    bet_size = kelly_bet(req.confidence, bankroll, req.signal_quality or "MODERADA")

    token_id = req.token_up if req.direction == "UP" else req.token_down
    if not token_id:
        token_id = await get_token_id(req.direction, cur_slot)
    if not token_id:
        raise HTTPException(404, "No se encontró mercado BTC 5m activo")

    client = get_poly_client()

    if not client:
        trade_entry = {
            "id": f"SIM-{int(time.time())}", "simulated": True,
            "direction": req.direction, "confidence": req.confidence,
            "bet_usd": bet_size, "token_id": token_id,
            "slot": cur_slot, "hour": now_hour,
            "ts": datetime.now(timezone.utc).isoformat(), "status": "PENDING",
        }
        trade_log.append(trade_entry)
        session_stats["trades"] += 1
        session_stats["pending"] += 1
        return {"executed": True, "simulated": True, "trade": trade_entry}

    try:
        from py_clob_client.clob_types import OrderArgs
        order_args = OrderArgs(token_id=token_id, price=0.5, size=bet_size, side="BUY")
        resp = client.create_and_post_order(order_args)
        trade_entry = {
            "id": resp.get("orderID", f"ORD-{int(time.time())}"), "simulated": False,
            "direction": req.direction, "confidence": req.confidence,
            "bet_usd": bet_size, "token_id": token_id,
            "slot": cur_slot, "hour": now_hour,
            "ts": datetime.now(timezone.utc).isoformat(), "status": resp.get("status", "PENDING"),
        }
        trade_log.append(trade_entry)
        session_stats["trades"] += 1
        session_stats["pending"] += 1
        return {"executed": True, "simulated": False, "trade": trade_entry}
    except Exception as e:
        raise HTTPException(500, f"Error ejecutando orden: {str(e)}")

@app.get("/status")
async def get_status():
    completed = session_stats["wins"] + session_stats["losses"]
    wr = round(session_stats["wins"] / completed * 100, 1) if completed > 0 else 0
    poly_ready = get_poly_client() is not None
    return {
        "online": True, "version": "2.0.0", "poly_connected": poly_ready,
        "config": {"max_bet_usd": MAX_BET_USD, "min_confidence": MIN_CONFIDENCE, "kelly_fraction": KELLY_FRACTION, "bad_hours": BAD_HOURS},
        "session": {**session_stats, "win_rate": wr, "completed": completed},
    }

@app.get("/history")
async def get_history(limit: int = 50):
    return {"trades": trade_log[-limit:][::-1], "total": len(trade_log)}

@app.get("/polymarket")
async def get_polymarket():
    cur_slot = (int(time.time()) // 300) * 300
    async with httpx.AsyncClient(timeout=10) as client:
        for s in [f"btc-updown-5m-{cur_slot}", f"btc-updown-5m-{cur_slot-300}", f"btc-updown-5m-{cur_slot+300}"]:
            try:
                r = await client.get(f"{GAMMA_BASE}/events?slug={s}")
                if r.status_code != 200: continue
                events = r.json()
                if not events: continue
                event = events[0]
                market = event.get("markets", [{}])[0]
                prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                tokens = json.loads(market.get("clobTokenIds", "[]"))
                return {
                    "question": event.get("title", s),
                    "upPct": round(float(prices[0]) * 100, 1),
                    "downPct": round(float(prices[1]) * 100, 1),
                    "volume": float(market.get("volume", 0)),
                    "slug": s,
                    "url": f"https://polymarket.com/event/{s}",
                    "tokenUp": tokens[0] if len(tokens) > 0 else None,
                    "tokenDown": tokens[1] if len(tokens) > 1 else None,
                }
            except Exception:
                continue
    raise HTTPException(404, "No se encontró mercado activo")

@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time())}
