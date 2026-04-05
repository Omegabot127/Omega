"""
OMEGA Backend v1.0
- /claude     → proxy al API de Anthropic (predicciones IA)
- /trade      → ejecuta órdenes en Polymarket CLOB
- /status     → estado del bot y balance
- /history    → historial de trades ejecutados
"""

import os, time, hmac, hashlib, json, asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ─── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
POLY_API_KEY      = os.getenv("POLY_API_KEY", "")
POLY_SECRET       = os.getenv("POLY_SECRET", "")
POLY_PASSPHRASE   = os.getenv("POLY_PASSPHRASE", "")
POLY_WALLET       = os.getenv("POLY_WALLET", "")

CLOB_BASE    = "https://clob.polymarket.com"
GAMMA_BASE   = "https://gamma-api.polymarket.com"
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# Gestión de riesgo — ajustable vía ENV
MAX_BET_USD       = float(os.getenv("MAX_BET_USD", "5.0"))      # máximo por trade
MIN_CONFIDENCE    = int(os.getenv("MIN_CONFIDENCE", "65"))       # mínimo confidence para apostar
MIN_BET_USD       = float(os.getenv("MIN_BET_USD", "1.0"))       # mínimo apuesta
KELLY_FRACTION    = float(os.getenv("KELLY_FRACTION", "0.25"))   # fracción Kelly conservadora
BAD_HOURS         = [int(h) for h in os.getenv("BAD_HOURS", "0,4,8,9,11,12,16,22,23").split(",")]

app = FastAPI(title="OMEGA Backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Trade log en memoria (persiste mientras corre el proceso) ────────────────
trade_log: list[dict] = []
session_stats = {"trades": 0, "wins": 0, "losses": 0, "pending": 0, "pnl": 0.0}

# ─── Models ───────────────────────────────────────────────────────────────────
class ClaudeRequest(BaseModel):
    prompt: str

class TradeRequest(BaseModel):
    direction: str          # "UP" o "DOWN"
    confidence: int         # 0-100
    token_up: Optional[str] = None
    token_down: Optional[str] = None
    bankroll: Optional[float] = None   # balance actual del usuario
    signal_quality: Optional[str] = "MODERADA"
    hour: Optional[int] = None

# ─── Helpers ──────────────────────────────────────────────────────────────────
def poly_headers(method: str, path: str, body: str = "") -> dict:
    """Genera headers firmados para Polymarket CLOB API."""
    ts = str(int(time.time() * 1000))
    msg = ts + method.upper() + path + (body if body else "")
    sig = hmac.new(POLY_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "POLY-API-KEY":        POLY_API_KEY,
        "POLY-SIGNATURE":      sig,
        "POLY-TIMESTAMP":      ts,
        "POLY-PASSPHRASE":     POLY_PASSPHRASE,
        "Content-Type":        "application/json",
    }

def kelly_bet(confidence: int, bankroll: float, signal_quality: str) -> float:
    """
    Kelly Criterion conservador:
    f = (p*(b+1) - 1) / b
    donde p = probabilidad de ganar, b = odds netas (≈1 en Polymarket binary)
    """
    p = confidence / 100.0
    b = 1.0  # binary market paga ~1:1
    kelly_f = max(0, (p * (b + 1) - 1) / b)

    # Fracción conservadora del Kelly completo
    bet = bankroll * kelly_f * KELLY_FRACTION

    # Ajuste por calidad de señal
    sq_mult = {"FUERTE": 1.0, "MODERADA": 0.7, "DÉBIL": 0.4}.get(signal_quality, 0.5)
    bet = bet * sq_mult

    # Clamp
    bet = max(MIN_BET_USD, min(MAX_BET_USD, bet))
    return round(bet, 2)

def is_bad_hour(hour: Optional[int]) -> bool:
    h = hour if hour is not None else datetime.now(timezone.utc).hour
    return h in BAD_HOURS

# ─── /claude ──────────────────────────────────────────────────────────────────
@app.post("/claude")
async def claude_proxy(req: ClaudeRequest):
    """Proxy al API de Anthropic. El HTML original llama aquí."""
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
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)

    data = r.json()
    text = data["content"][0]["text"] if data.get("content") else ""
    return {"text": text}


# ─── /trade ───────────────────────────────────────────────────────────────────
@app.post("/trade")
async def execute_trade(req: TradeRequest):
    """
    Valida la señal y ejecuta una orden en Polymarket CLOB.
    Retorna el resultado del trade o el motivo de rechazo.
    """
    now_hour = req.hour if req.hour is not None else datetime.now(timezone.utc).hour
    cur_slot = (int(time.time()) // 300) * 300

    # ── 1. Filtros de riesgo ──────────────────────────────────────────────────
    if req.direction == "NEUTRAL":
        return {"executed": False, "reason": "señal NEUTRAL — no se apuesta"}

    if req.confidence < MIN_CONFIDENCE:
        return {"executed": False, "reason": f"confianza {req.confidence}% < mínimo {MIN_CONFIDENCE}%"}

    if is_bad_hour(now_hour):
        return {"executed": False, "reason": f"hora {now_hour}h en lista negra (WR histórico bajo)"}

    if req.signal_quality == "DÉBIL":
        return {"executed": False, "reason": "señal DÉBIL — esperando señal más fuerte"}

    # ── 2. Calcular apuesta ───────────────────────────────────────────────────
    bankroll = req.bankroll or 50.0
    bet_size = kelly_bet(req.confidence, bankroll, req.signal_quality or "MODERADA")

    # ── 3. Obtener token ID del mercado activo ────────────────────────────────
    token_id = req.token_up if req.direction == "UP" else req.token_down

    if not token_id:
        # Buscar automáticamente en Polymarket
        slug = f"btc-updown-5m-{cur_slot}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{GAMMA_BASE}/events?slug={slug}")
        if r.status_code != 200:
            raise HTTPException(502, f"No se pudo obtener mercado: {r.text}")
        events = r.json()
        if not events:
            raise HTTPException(404, f"Mercado {slug} no encontrado")
        market = events[0].get("markets", [{}])[0]
        try:
            tokens = json.loads(market.get("clobTokenIds", "[]"))
            token_id = tokens[0] if req.direction == "UP" else tokens[1]
        except Exception:
            raise HTTPException(500, "No se pudieron parsear token IDs")

    if not token_id:
        raise HTTPException(400, "token_id no disponible")

    # ── 4. Ejecutar orden en CLOB ─────────────────────────────────────────────
    if not POLY_API_KEY:
        # Modo simulación si no hay claves
        trade_entry = {
            "id": f"SIM-{int(time.time())}",
            "simulated": True,
            "direction": req.direction,
            "confidence": req.confidence,
            "bet_usd": bet_size,
            "token_id": token_id,
            "slot": cur_slot,
            "hour": now_hour,
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": "PENDING",
        }
        trade_log.append(trade_entry)
        session_stats["trades"] += 1
        session_stats["pending"] += 1
        return {"executed": True, "simulated": True, "trade": trade_entry}

    # Orden real: market order (FOK) al precio de mercado
    order_body = json.dumps({
        "tokenID": token_id,
        "price":   0.5,          # precio límite — se ajusta al mercado
        "size":    bet_size,
        "side":    "BUY",
        "orderType": "FOK",      # Fill-or-Kill
        "timeInForce": "FOK",
    })

    path = "/order"
    headers = poly_headers("POST", path, order_body)

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{CLOB_BASE}{path}", headers=headers, content=order_body)

    if r.status_code not in (200, 201):
        raise HTTPException(r.status_code, f"CLOB error: {r.text}")

    order_resp = r.json()
    trade_entry = {
        "id": order_resp.get("orderID", f"ORD-{int(time.time())}"),
        "simulated": False,
        "direction": req.direction,
        "confidence": req.confidence,
        "bet_usd": bet_size,
        "token_id": token_id,
        "slot": cur_slot,
        "hour": now_hour,
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": order_resp.get("status", "PENDING"),
        "raw": order_resp,
    }
    trade_log.append(trade_entry)
    session_stats["trades"] += 1
    session_stats["pending"] += 1

    return {"executed": True, "simulated": False, "trade": trade_entry}


# ─── /status ──────────────────────────────────────────────────────────────────
@app.get("/status")
async def get_status():
    """Estado del bot: configuración activa y stats de sesión."""
    wr = 0
    completed = session_stats["wins"] + session_stats["losses"]
    if completed > 0:
        wr = round(session_stats["wins"] / completed * 100, 1)

    return {
        "online": True,
        "version": "1.0.0",
        "config": {
            "max_bet_usd": MAX_BET_USD,
            "min_confidence": MIN_CONFIDENCE,
            "kelly_fraction": KELLY_FRACTION,
            "bad_hours": BAD_HOURS,
            "auto_trade": bool(POLY_API_KEY),
        },
        "session": {
            **session_stats,
            "win_rate": wr,
            "completed": completed,
        },
    }


# ─── /history ─────────────────────────────────────────────────────────────────
@app.get("/history")
async def get_history(limit: int = 50):
    return {"trades": trade_log[-limit:][::-1], "total": len(trade_log)}


# ─── /health ──────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time())}
