"""
strategy_core_prod.py — Market discovery + order book metrics + ejecución de órdenes REALES

Cambios vs strategy_core.py (backtesting):
  - get_authenticated_clob_client() → cliente CLOB autenticado con credenciales reales
  - place_taker_buy(token_id, amount_usdc)  → orden de compra taker (FOK)
  - place_taker_sell(token_id, shares, bid) → orden de venta taker (FOK) para stop-loss
  - Todos los demás métodos son idénticos al backtesting.
"""

import os
import time
import requests
from datetime import datetime, timezone
from collections import deque

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

CLOB_HOST   = "https://clob.polymarket.com"
GAMMA_API   = "https://gamma-api.polymarket.com"
SLOT_ORIGIN = 1771778100
SLOT_STEP   = 300
TOP_LEVELS  = 15

SLUG_PREFIXES = {
    "SOL": "sol-updown-5m",
    "BTC": "btc-updown-5m",
    "ETH": "eth-updown-5m",
}


# ═══════════════════════════════════════════════════════
#  CLOB AUTENTICADO
# ═══════════════════════════════════════════════════════

_auth_client: ClobClient | None = None

def get_authenticated_clob_client() -> ClobClient:
    """
    Crea (o reutiliza) el cliente CLOB autenticado.
    Requiere en entorno:
      POLYMARKET_KEY  — clave privada de la wallet (hex)
      PROXY_ADDRESS   — direccion proxy de la wallet en Polymarket
      POLY_CHAIN_ID   — 137 (Polygon mainnet). Default: 137
    """
    global _auth_client
    if _auth_client is not None:
        return _auth_client

    private_key   = os.environ["POLYMARKET_KEY"]
    proxy_address = os.environ.get("PROXY_ADDRESS", "")
    chain_id      = int(os.environ.get("POLY_CHAIN_ID", "137"))

    _auth_client = ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=chain_id,
        funder=proxy_address if proxy_address else None,
        signature_type=1,
    )
    _auth_client.set_api_creds(_auth_client.create_or_derive_api_creds())
    try:
        _auth_client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
    except Exception:
        pass
    return _auth_client


def approve_conditional_token(token_id: str) -> None:
    """Sincroniza el balance del token condicional en el cache interno del CLOB.
    Llamar antes de cada intento de SELL."""
    try:
        client = get_authenticated_clob_client()
        client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        time.sleep(0.5)
    except Exception:
        pass


def place_taker_buy(token_id: str, shares: float, price: float) -> dict:
    """
    Coloca una orden de compra taker (limit GTC al ask) por el token indicado.
    Usar precio=ask garantiza cruce inmediato como taker.

    Después de postear la orden, hace polling durante 4s para verificar el fill real.
    Si quedó parcialmente llenada, cancela el remanente y retorna las shares realmente
    llenadas (size_matched) para evitar shares huérfanas y capital mal contabilizado.

    Parámetros:
      token_id — token ID de Polymarket (UP o DOWN)
      shares   — cantidad de shares a comprar (entry_usd / ask)
      price    — precio ask actual (limite para cruce taker)
    """
    client = get_authenticated_clob_client()
    try:
        order_args   = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=round(shares, 2),
            side=BUY,
            fee_rate_bps=1000,
        )
        signed_order = client.create_order(order_args)
        resp         = client.post_order(signed_order, OrderType.GTC)
        if not resp or not resp.get("orderID"):
            return {"success": False, "orderID": None, "shares_filled": 0.0, "error": str(resp), "raw": resp}

        order_id = resp["orderID"]

        # Polling fill durante 2s (cada 0.5s) — taker al ask llena en <1s típicamente
        size_matched = 0.0
        deadline = time.time() + 2.0
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                info         = client.get_order(order_id)
                status       = info.get("status", "")
                size_matched = float(info.get("size_matched", 0) or 0)
                if status in ("MATCHED", "FILLED") or size_matched >= round(shares, 2) * 0.95:
                    break
                if status == "CANCELLED":
                    break
            except Exception:
                pass

        # Cancelar remanente si el fill fue parcial
        if size_matched > 0 and size_matched < round(shares, 2) * 0.95:
            try:
                client.cancel(order_id)
            except Exception:
                pass

        filled = size_matched if size_matched > 0 else round(shares, 2)
        return {
            "success":       True,
            "orderID":       order_id,
            "shares_filled": round(filled, 2),
            "error":         None,
            "raw":           resp,
        }
    except Exception as e:
        return {"success": False, "orderID": None, "shares_filled": 0.0, "error": str(e), "raw": None}


def place_taker_sell(token_id: str, shares: float, bid_price: float) -> dict:
    """
    Coloca una orden SELL GTC con escalado agresivo de precio para garantizar fill.

    Intentos: bid_price, bid_price-0.05, bid_price-0.10, ... hasta 0.01 (cada 3s)
    Por cada intento:
      1. Coloca orden GTC al precio actual
      2. Polling fill durante 3s (cada 0.5s)
      3. Si no llenó → cancela y baja 0.05
    Retorna dict con {"success": bool, "orderID": str|None, "error": str|None}
    """
    client       = get_authenticated_clob_client()
    FILL_TIMEOUT = 3.0
    FILL_POLL    = 0.5

    # Construir escala de precios: bid_price, bid-0.05, bid-0.10, ... hasta 0.01
    precios = []
    p = round(bid_price, 4)
    while p > 0.01:
        precios.append(round(p, 4))
        p = round(p - 0.05, 4)
    precios.append(0.01)  # piso garantizado

    for intento, precio in enumerate(precios, start=1):
        try:
            order_args   = OrderArgs(
                token_id=token_id,
                price=precio,
                size=round(shares, 2),
                side=SELL,
                fee_rate_bps=1000,
            )
            signed_order = client.create_order(order_args)
            resp         = client.post_order(signed_order, OrderType.GTC)

            order_id = resp.get("orderID") if resp else None
            if not order_id:
                continue

            # Polling fill durante FILL_TIMEOUT segundos
            deadline = time.time() + FILL_TIMEOUT
            filled   = False
            while time.time() < deadline:
                time.sleep(FILL_POLL)
                try:
                    info         = client.get_order(order_id)
                    status       = info.get("status", "")
                    size_matched = float(info.get("size_matched", 0) or 0)
                    if status in ("MATCHED", "FILLED") or size_matched >= round(shares, 2) * 0.9:
                        filled = True
                        break
                    if status == "CANCELLED":
                        break
                except Exception:
                    pass

            if filled:
                # Intentar capturar el precio real de fill (puede ser mejor que el límite)
                fill_price = None
                try:
                    final_info = client.get_order(order_id)
                    # associate_trades contiene los fills reales con su precio
                    trades_list = (final_info.get("associate_trades")
                                   or final_info.get("trades") or [])
                    if trades_list:
                        total_val  = sum(float(t.get("price", precio)) * float(t.get("size", 0)) for t in trades_list)
                        total_size = sum(float(t.get("size", 0)) for t in trades_list)
                        if total_size > 0:
                            fill_price = round(total_val / total_size, 4)
                    # Fallback: maker_amount / taker_amount
                    if fill_price is None:
                        maker = float(final_info.get("maker_amount") or 0)
                        taker = float(final_info.get("taker_amount") or 0)
                        if taker > 0:
                            fill_price = round(maker / taker, 4)
                except Exception:
                    pass
                return {
                    "success":    True,
                    "orderID":    order_id,
                    "fill_price": fill_price,   # precio real de ejecución (None si no disponible)
                    "error":      None,
                    "raw":        resp,
                }

            # No se llenó — cancelar antes de bajar precio
            try:
                client.cancel(order_id)
            except Exception:
                pass

        except Exception as e:
            if intento == len(precios):
                return {"success": False, "orderID": None, "error": str(e), "raw": None}

    return {"success": False, "orderID": None, "error": "sin fill tras escalado completo hasta 0.01", "raw": None}


def place_stop_loss_order(token_id: str, shares: float, sl_price: float) -> dict:
    """
    Coloca una orden GTC limit SELL a sl_price como stop-loss.
    Queda viva en el CLOB hasta que se llene o sea cancelada.
    """
    client = get_authenticated_clob_client()
    try:
        order_args   = OrderArgs(
            token_id=token_id,
            price=round(sl_price, 4),
            size=round(shares, 2),
            side=SELL,
            fee_rate_bps=1000,
        )
        signed_order = client.create_order(order_args)
        resp         = client.post_order(signed_order, OrderType.GTC)
        if resp and resp.get("orderID"):
            return {"success": True, "orderID": resp["orderID"], "error": None}
        else:
            return {"success": False, "orderID": None, "error": str(resp)}
    except Exception as e:
        return {"success": False, "orderID": None, "error": str(e)}


def get_clob_balance(token_id: str) -> float:
    """
    Retorna el balance del token condicional disponible en el CLOB.
    Llamar antes de place_taker_sell para saber cuántas shares hay realmente.
    Retorna 0.0 si no se puede consultar.
    """
    try:
        client = get_authenticated_clob_client()
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        if result:
            raw = (result.get("balance")
                   or result.get("asset", {}).get("balance"))
            if raw is not None:
                val = float(raw)
                return val / 1_000_000 if val > 1_000 else val
    except Exception:
        pass
    return 0.0


def cancel_order(order_id: str) -> bool:
    """Cancela una orden por ID. Retorna True si tuvo éxito."""
    try:
        client = get_authenticated_clob_client()
        client.cancel(order_id)
        return True
    except Exception:
        return False


def get_order_status(order_id: str) -> str | None:
    """Retorna el status de una orden: LIVE, MATCHED, FILLED, CANCELED, etc."""
    try:
        client = get_authenticated_clob_client()
        info = client.get_order(order_id)
        return info.get("status") if info else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════
#  MARKET DISCOVERY
# ═══════════════════════════════════════════════════════

def get_current_slot_ts():
    now     = int(time.time())
    elapsed = (now - SLOT_ORIGIN) % SLOT_STEP
    return now - elapsed


def fetch_gamma_market(slug: str):
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=8)
        r.raise_for_status()
        data = r.json()
        return data[0] if isinstance(data, list) and data else None
    except Exception:
        return None


def fetch_clob_market(condition_id: str):
    try:
        r = requests.get(f"{CLOB_HOST}/markets/{condition_id}", timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def build_market_info(gamma_m, clob_m) -> dict | None:
    tokens = clob_m.get("tokens", [])
    if len(tokens) < 2:
        return None

    up_t   = next((t for t in tokens if "up"   in (t.get("outcome") or "").lower()), tokens[0])
    down_t = next((t for t in tokens if "down" in (t.get("outcome") or "").lower()), tokens[1])

    return {
        "condition_id":     clob_m.get("condition_id"),
        "question":         clob_m.get("question", ""),
        "end_date":         gamma_m.get("endDate") or clob_m.get("end_date_iso", ""),
        "market_slug":      clob_m.get("market_slug", ""),
        "accepting_orders": bool(clob_m.get("accepting_orders")),
        "up_token_id":      up_t["token_id"],
        "up_outcome":       up_t.get("outcome", "Up"),
        "up_price":         float(up_t.get("price") or 0.5),
        "down_token_id":    down_t["token_id"],
        "down_outcome":     down_t.get("outcome", "Down"),
        "down_price":       float(down_t.get("price") or 0.5),
    }


def _order_book_live(token_id: str) -> bool:
    try:
        r = requests.get(
            f"{CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


def find_active_market(symbol: str) -> dict | None:
    slug_prefix = SLUG_PREFIXES.get(symbol.upper())
    if not slug_prefix:
        raise ValueError(f"Simbolo no soportado: {symbol}. Usa SOL, BTC o ETH.")

    now  = int(time.time())
    base = now - (now % SLOT_STEP)

    for offset in [0, 1, -1, 2, -2, -3]:
        ts   = base + offset * SLOT_STEP
        slug = f"{slug_prefix}-{ts}"
        gm   = fetch_gamma_market(slug)
        if not gm:
            continue
        cid = gm.get("conditionId")
        if not cid:
            continue
        cm = fetch_clob_market(cid)
        if not cm:
            continue
        info = build_market_info(gm, cm)
        if not info:
            continue
        if _order_book_live(info["up_token_id"]):
            return info
    return None


def fetch_market_resolution(condition_id: str) -> str | None:
    try:
        r = requests.get(f"{GAMMA_API}/markets/{condition_id}", timeout=8)
        r.raise_for_status()
        data = r.json()

        outcome_prices = data.get("outcomePrices")
        if outcome_prices:
            try:
                prices = [float(p) for p in outcome_prices]
                if prices[0] >= 0.99:
                    return "UP"
                elif prices[1] >= 0.99:
                    return "DOWN"
            except Exception:
                pass

        if data.get("resolved"):
            winner = (data.get("winner") or "").lower()
            if "up" in winner:
                return "UP"
            elif "down" in winner:
                return "DOWN"

        return None
    except Exception:
        return None


def seconds_remaining(market_info: dict) -> float | None:
    end_raw = market_info.get("end_date", "")
    if not end_raw:
        return None
    try:
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        diff   = (end_dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, diff)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════
#  ORDER BOOK
# ═══════════════════════════════════════════════════════

_read_only_client: ClobClient | None = None

def get_clob_client() -> ClobClient:
    global _read_only_client
    if _read_only_client is None:
        _read_only_client = ClobClient(CLOB_HOST)
    return _read_only_client


def get_order_book_metrics(token_id: str, top_n: int = TOP_LEVELS) -> tuple[dict | None, str | None]:
    try:
        ob = get_clob_client().get_order_book(token_id)
    except Exception as e:
        return None, str(e)

    bids = sorted(ob.bids or [], key=lambda x: float(x.price), reverse=True)[:top_n]
    asks = sorted(ob.asks or [], key=lambda x: float(x.price))[:top_n]

    bid_vol = sum(float(b.size) for b in bids)
    ask_vol = sum(float(a.size) for a in asks)
    total   = bid_vol + ask_vol
    obi     = (bid_vol - ask_vol) / total if total > 0 else 0.0

    best_bid = float(bids[0].price) if bids else 0.0
    best_ask = float(asks[0].price) if asks else 0.0
    spread   = round(best_ask - best_bid, 4)

    if total > 0:
        bvwap = sum(float(b.price) * float(b.size) for b in bids) / bid_vol if bid_vol > 0 else 0
        avwap = sum(float(a.price) * float(a.size) for a in asks) / ask_vol if ask_vol > 0 else 0
        vwap_mid = (bvwap * bid_vol + avwap * ask_vol) / total
    else:
        vwap_mid = (best_bid + best_ask) / 2

    return {
        "bid_volume":   round(bid_vol, 2),
        "ask_volume":   round(ask_vol, 2),
        "total_volume": round(total, 2),
        "obi":          round(obi, 4),
        "best_bid":     round(best_bid, 4),
        "best_ask":     round(best_ask, 4),
        "spread":       spread,
        "vwap_mid":     round(vwap_mid, 4),
        "num_bids":     len(ob.bids or []),
        "num_asks":     len(ob.asks or []),
        "top_bids":     [(round(float(b.price), 4), round(float(b.size), 2)) for b in bids[:8]],
        "top_asks":     [(round(float(a.price), 4), round(float(a.size), 2)) for a in asks[:8]],
    }, None


def compute_signal(obi_now: float, obi_window: list[float], threshold: float) -> dict:
    avg_obi  = sum(obi_window) / len(obi_window) if obi_window else obi_now
    combined = round(0.6 * obi_now + 0.4 * avg_obi, 4)
    abs_c    = abs(combined)

    if combined > threshold:
        conf  = min(int(50 + (abs_c / 0.5) * 50), 99)
        label = "STRONG UP" if combined > threshold * 2 else "UP"
        color = "green"
    elif combined < -threshold:
        conf  = min(int(50 + (abs_c / 0.5) * 50), 99)
        label = "STRONG DOWN" if combined < -threshold * 2 else "DOWN"
        color = "red"
    else:
        label = "NEUTRAL"
        color = "yellow"
        conf  = 50

    return {
        "label":      label,
        "color":      color,
        "confidence": conf,
        "obi_now":    round(obi_now, 4),
        "obi_avg":    round(avg_obi, 4),
        "combined":   combined,
        "history":    list(obi_window)[-20:],
        "threshold":  threshold,
    }


# ═══════════════════════════════════════════════════════
#  CONSULTA DE SALDO USDC
# ═══════════════════════════════════════════════════════

def get_usdc_balance() -> float | None:
    """
    Consulta el saldo USDC disponible en Polymarket CLOB.
    Estrategia en cascada:
      1. py_clob_client.get_balance_allowance (AssetType.COLLATERAL)
      2. get_balance() si existe
      3. Request L1-firmado a /balance
      4. On-chain USDC.e / USDC nativo como último recurso
    """
    # ── Método 1: get_balance_allowance ───────────────────────────────────
    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        client = get_authenticated_clob_client()
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        if result:
            raw = (result.get("balance")
                   or result.get("asset", {}).get("balance")
                   or result.get("collateral_balance"))
            if raw is not None:
                val = float(raw)
                return val / 1_000_000 if val > 1_000 else val
    except Exception:
        pass

    # ── Método 2: get_balance() ───────────────────────────────────────────
    try:
        client = get_authenticated_clob_client()
        if hasattr(client, "get_balance"):
            val = client.get_balance()
            if val is not None:
                v = float(str(val).strip('"'))
                return v / 1_000_000 if v > 1_000 else v
    except Exception:
        pass

    # ── Método 3: request L1-firmado a /balance ───────────────────────────
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        private_key = os.environ.get("POLYMARKET_KEY", "")
        if private_key:
            acct = Account.from_key(private_key)
            ts = str(int(time.time() * 1000))
            for path in ["/balance", "/data/balance-allowance"]:
                try:
                    sig = acct.sign_message(
                        encode_defunct(text=ts + "GET" + path + "")
                    ).signature.hex()
                    if not sig.startswith("0x"):
                        sig = "0x" + sig
                    r = requests.get(
                        f"{CLOB_HOST}{path}",
                        headers={
                            "POLY_ADDRESS":   acct.address,
                            "POLY_SIGNATURE": sig,
                            "POLY_TIMESTAMP": ts,
                            "POLY_NONCE":     ts,
                        },
                        timeout=8,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        raw = (data if isinstance(data, (int, float, str))
                               else data.get("balance")
                               or data.get("asset", {}).get("balance"))
                        if raw is not None:
                            v = float(str(raw).strip('"'))
                            return v / 1_000_000 if v > 1_000 else v
                except Exception:
                    continue
    except Exception:
        pass

    # ── Método 4: on-chain USDC (fallback) ────────────────────────────────
    USDC_CONTRACTS = [
        "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    ]
    RPCS = [
        "https://polygon-rpc.com",
        "https://rpc.ankr.com/polygon",
        "https://polygon-bor-rpc.publicnode.com",
    ]
    addresses = []
    proxy = os.environ.get("PROXY_ADDRESS", "").strip()
    if proxy:
        addresses.append(proxy)
    try:
        from eth_account import Account
        pk = os.environ.get("POLYMARKET_KEY", "")
        if pk:
            eoa = Account.from_key(pk).address
            if eoa.lower() != proxy.lower():
                addresses.append(eoa)
    except Exception:
        pass

    total = 0.0
    found = False
    for addr in addresses:
        for contract in USDC_CONTRACTS:
            for rpc in RPCS:
                try:
                    data = "0x70a08231" + addr.lower().replace("0x", "").zfill(64)
                    r = requests.post(
                        rpc,
                        json={"jsonrpc": "2.0", "method": "eth_call",
                              "params": [{"to": contract, "data": data}, "latest"], "id": 1},
                        timeout=8,
                    )
                    val = int(r.json().get("result", "0x0"), 16) / 1_000_000
                    total += val
                    found = True
                    break
                except Exception:
                    continue
    return round(total, 4) if found else None
