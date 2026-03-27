"""
hedge_live_prod.py — Hedge Dinámico BTC Up/Down 5m  *** PRODUCCIÓN REAL ***

Estrategia idéntica a hedge_live.py (SIM-V3 v9) pero con órdenes reales en Polymarket CLOB.

Diferencias vs simulación:
  - comprar()       → place_taker_buy()  al ask real
  - forzar_salida() → approve_conditional_token() + place_taker_sell() al bid real
  - resolución      → vende lado ganador al bid (cerca de 1.0) antes de redención
  - capital inicial → sincronizado desde get_usdc_balance() al arrancar

Variables de entorno requeridas:
  POLYMARKET_KEY   — clave privada hex de la wallet
  PROXY_ADDRESS    — dirección proxy en Polymarket
  POLY_CHAIN_ID    — 137 (default)
  CAPITAL_INICIAL  — capital de referencia (solo para ROI / drawdown tracking)
  STATE_FILE       — path archivo estado  (default: /app/data/state.json)
  LOG_FILE         — path archivo log     (default: /app/data/hedge_log.json)
  EVENTS_FILE      — path eventos         (default: /app/data/events.log)
  PORT             — puerto HTTP          (default: 8080)
"""

import asyncio
import os
import sys
import time
import json
import logging
from datetime import datetime, timezone
from collections import deque

from strategy_core_prod import (
    find_active_market,
    get_order_book_metrics,
    compute_signal,
    seconds_remaining,
    place_taker_buy,
    place_taker_sell,
    approve_conditional_token,
    get_clob_balance,
    get_usdc_balance,
)

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("hedge_prod")
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ─── CONFIG DESDE ENV VARS ────────────────────────────────────────────────────
CAPITAL_INICIAL = float(os.environ.get("CAPITAL_INICIAL", "100.0"))
STATE_FILE      = os.environ.get("STATE_FILE",  "/app/data/state.json")
LOG_FILE        = os.environ.get("LOG_FILE",    "/app/data/hedge_log.json")
EVENTS_FILE     = os.environ.get("EVENTS_FILE", "/app/data/events.log")

# ─── PARÁMETROS ───────────────────────────────────────────────────────────────
MONTO_FIJO_POR_LADO  = float(os.environ.get("ENTRY_USD", "3.75"))
POLL_INTERVAL        = 1.0
OBI_THRESHOLD        = 0.10
OBI_WINDOW_SIZE      = 8
OBI_STRONG_THRESHOLD = 0.20
SPREAD_MAX           = 0.12
PRECIO_MIN_LADO1     = 0.35
PRECIO_MAX_LADO1     = 0.50
ENTRY_WINDOW_MAX     = 240
ENTRY_WINDOW_MIN     = 60
HEDGE_MOVE_MIN       = 0.05
HEDGE_OBI_MIN        = -0.05
HEDGE_PRECIO_MIN     = 0.25
HEDGE_PRECIO_MAX     = 0.35
EARLY_EXIT_SECS      = 60
EARLY_EXIT_OBI_FLIP  = -0.15
EARLY_EXIT_PRICE_DROP = 0.08
RESOLVED_UP_THRESH   = 0.97
RESOLVED_DN_THRESH   = 0.03
MIN_USD_ORDEN        = 1.00
MIN_HOLD_SECS        = 10
NEAR_RESOLUTION_THRESH = 0.82
NEAR_RESOLUTION_SECS   = 90

# ─── ESTADO GLOBAL ────────────────────────────────────────────────────────────
PAUSED = True

estado = {
    "capital":      CAPITAL_INICIAL,
    "pnl_total":    0.0,
    "peak_capital": CAPITAL_INICIAL,
    "max_drawdown": 0.0,
    "wins":         0,
    "losses":       0,
    "ciclos":       0,
    "trades":       [],
}

obi_history_up = deque(maxlen=OBI_WINDOW_SIZE)
obi_history_dn = deque(maxlen=OBI_WINDOW_SIZE)

pos = {
    "activa":           False,
    "lado1_side":       None,
    "lado1_token_id":   None,   # token_id real para órdenes CLOB
    "lado1_precio":     0.0,
    "lado1_shares":     0.0,
    "lado1_usd":        0.0,
    "lado2_side":       None,
    "lado2_token_id":   None,   # token_id real para órdenes CLOB
    "lado2_precio":     0.0,
    "lado2_shares":     0.0,
    "lado2_usd":        0.0,
    "hedgeado":         False,
    "capital_usado":    0.0,
    "ts_entrada":       None,
    "secs_entrada":     0.0,
    # ── sistema de reintentos de venta ────────────────────────────────────
    "salida_pendiente":  False,  # True si hay una venta que no se pudo ejecutar
    "salida_retries":    0,
    "salida_tipo":       None,   # "EARLY_EXIT" | "RESOLUTION"
    "salida_resolucion": None,   # "UP"|"DOWN" — solo para RESOLUTION
    "salida_razon":      "",     # razon del early exit
}

eventos      = deque(maxlen=200)
mkt_end_date = None
_ob_error_count = 0


# ─── PERSISTENCIA ─────────────────────────────────────────────────────────────

def _makedirs(filepath):
    d = os.path.dirname(filepath)
    if d:
        os.makedirs(d, exist_ok=True)


def guardar_estado(up_m=None, dn_m=None):
    total = estado["wins"] + estado["losses"]
    wr    = estado["wins"] / total * 100 if total > 0 else 0.0
    roi   = (estado["capital"] - CAPITAL_INICIAL) / CAPITAL_INICIAL * 100

    ob_up = {
        "ask": round(up_m["best_ask"], 4),
        "bid": round(up_m["best_bid"], 4),
        "obi": round(up_m["obi"], 4),
    } if up_m else None

    ob_dn = {
        "ask": round(dn_m["best_ask"], 4),
        "bid": round(dn_m["best_bid"], 4),
        "obi": round(dn_m["obi"], 4),
    } if dn_m else None

    try:
        _makedirs(STATE_FILE)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "ts":              datetime.now().isoformat(),
                "capital":         round(estado["capital"], 4),
                "capital_inicial": CAPITAL_INICIAL,
                "pnl_total":       round(estado["pnl_total"], 4),
                "roi":             round(roi, 2),
                "peak_capital":    round(estado["peak_capital"], 4),
                "max_drawdown":    round(estado["max_drawdown"], 4),
                "wins":            estado["wins"],
                "losses":          estado["losses"],
                "win_rate":        round(wr, 1),
                "ciclos":          estado["ciclos"],
                "ob_up":           ob_up,
                "ob_dn":           ob_dn,
                "paused":          PAUSED,
                "posicion": {
                    "activa":        pos["activa"],
                    "lado1":         pos["lado1_side"],
                    "lado2":         pos["lado2_side"],
                    "hedgeado":      pos["hedgeado"],
                    "capital_usado": round(pos["capital_usado"], 4),
                },
                "mkt_end_date": mkt_end_date,
                "eventos": list(eventos)[-30:],
                "trades":  estado["trades"][-20:],
            }, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning(f"guardar_estado error: {e}")

    try:
        _makedirs(LOG_FILE)
        with open(LOG_FILE, "w") as f:
            json.dump({
                "summary": {
                    "capital_inicial": CAPITAL_INICIAL,
                    "capital_actual":  round(estado["capital"], 4),
                    "pnl_total":       round(estado["pnl_total"], 4),
                    "roi_pct":         round(roi, 2),
                    "max_drawdown":    round(estado["max_drawdown"], 4),
                    "wins":            estado["wins"],
                    "losses":          estado["losses"],
                    "win_rate":        round(wr, 1),
                },
                "trades": estado["trades"],
            }, f, indent=2)
    except Exception as e:
        log.warning(f"guardar_log error: {e}")


def restaurar_estado():
    if not os.path.isfile(LOG_FILE):
        log.info("Sin estado previo — iniciando desde cero.")
        return
    try:
        with open(LOG_FILE) as f:
            data = json.load(f)
        s = data.get("summary", {})
        estado["capital"]   = float(s.get("capital_actual", CAPITAL_INICIAL))
        estado["pnl_total"] = float(s.get("pnl_total", 0.0))
        estado["wins"]      = int(s.get("wins", 0))
        estado["losses"]    = int(s.get("losses", 0))
        estado["trades"]    = data.get("trades", [])

        peak = CAPITAL_INICIAL
        for t in estado["trades"]:
            cap = float(t.get("capital", CAPITAL_INICIAL))
            if cap > peak:
                peak = cap
            dd = peak - cap
            if dd > estado["max_drawdown"]:
                estado["max_drawdown"] = dd
        estado["peak_capital"] = peak

        total = estado["wins"] + estado["losses"]
        log.info(
            f"Estado restaurado — {total} trades | "
            f"Capital: ${estado['capital']:.2f} | "
            f"PnL: ${estado['pnl_total']:+.2f} | "
            f"W:{estado['wins']} L:{estado['losses']}"
        )
    except Exception as e:
        log.warning(f"No se pudo restaurar estado: {e}")


def sincronizar_capital_clob():
    """Sincroniza el capital desde el saldo real USDC del CLOB al arrancar."""
    try:
        balance = get_usdc_balance()
        if balance is not None and balance > 0:
            log.info(f"Balance CLOB real: ${balance:.2f} USDC")
            estado["capital"] = balance
            if balance > estado["peak_capital"]:
                estado["peak_capital"] = balance
        else:
            log.warning("No se pudo obtener balance CLOB — usando capital de estado guardado.")
    except Exception as e:
        log.warning(f"sincronizar_capital_clob error: {e}")


# ─── UTILIDADES ───────────────────────────────────────────────────────────────

def log_ev(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    entrada = f"[{ts}] {msg}"
    eventos.append(entrada)
    log.info(msg)
    try:
        _makedirs(EVENTS_FILE)
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(entrada + "\n")
    except Exception:
        pass


def mid(m) -> float:
    b, a = m["best_bid"], m["best_ask"]
    if b > 0 and a > 0:
        return round((b + a) / 2, 4)
    return round(b or a, 4)


def actualizar_drawdown():
    cap = estado["capital"]
    if cap > estado["peak_capital"]:
        estado["peak_capital"] = cap
    dd = estado["peak_capital"] - cap
    if dd > estado["max_drawdown"]:
        estado["max_drawdown"] = dd


def resetear_pos():
    for k in pos:
        if k in ("activa", "hedgeado", "salida_pendiente"):
            pos[k] = False
        elif k == "salida_retries":
            pos[k] = 0
        elif isinstance(pos[k], str) or pos[k] is None:
            pos[k] = None
        else:
            pos[k] = 0.0


def imprimir_estado(up_m, dn_m, secs, signal_up, signal_dn):
    sep   = "-" * 65
    total = estado["wins"] + estado["losses"]
    wr    = estado["wins"] / total * 100 if total > 0 else 0
    roi   = (estado["capital"] - CAPITAL_INICIAL) / CAPITAL_INICIAL * 100

    print(f"\n{sep}")
    print(f"  {'[PAUSADO]' if PAUSED else '[ACTIVO] '} "
          f"Capital: ${estado['capital']:.2f}  PnL: ${estado['pnl_total']:+.2f}  "
          f"ROI: {roi:+.1f}%  MaxDD: ${estado['max_drawdown']:.2f}")
    print(f"  W:{estado['wins']} L:{estado['losses']} WR:{wr:.0f}%  |  Ciclos: {estado['ciclos']}")
    print(f"  Orden fija: ${MONTO_FIJO_POR_LADO:.2f}/lado  [PRODUCCION REAL]")

    if up_m and dn_m:
        print(f"  UP  bid={up_m['best_bid']:.3f} ask={up_m['best_ask']:.3f} "
              f"mid={mid(up_m):.3f}  OBI={up_m['obi']:+.3f} spread={up_m['spread']:.3f}")
        print(f"  DN  bid={dn_m['best_bid']:.3f} ask={dn_m['best_ask']:.3f} "
              f"mid={mid(dn_m):.3f}  OBI={dn_m['obi']:+.3f} spread={dn_m['spread']:.3f}")
        if signal_up:
            print(f"  Señal UP: {signal_up['label']} conf={signal_up['confidence']}%  "
                  f"combined={signal_up['combined']:+.3f}")
        if signal_dn:
            print(f"  Señal DN: {signal_dn['label']} conf={signal_dn['confidence']}%  "
                  f"combined={signal_dn['combined']:+.3f}")
        print(f"  Tiempo restante: {int(secs) if secs else '?'}s")

    if pos["activa"]:
        secs_en_pos = time.time() - pos["ts_entrada"] if pos["ts_entrada"] else 0
        print(f"\n  POSICION ABIERTA ({int(secs_en_pos)}s):")
        print(f"    Lado1: {pos['lado1_side']} @ {pos['lado1_precio']:.4f} | "
              f"${pos['lado1_usd']:.2f} | {pos['lado1_shares']:.4f}sh")
        if pos["hedgeado"]:
            print(f"    Lado2: {pos['lado2_side']} @ {pos['lado2_precio']:.4f} | "
                  f"${pos['lado2_usd']:.2f} | {pos['lado2_shares']:.4f}sh")
            print(f"    Capital en juego: ${pos['capital_usado']:.2f}")
        else:
            print(f"    Esperando hedge...")
    else:
        print(f"\n  Sin posicion abierta")
    print(sep)


# ─── COMPRA REAL al ask ────────────────────────────────────────────────────────

def comprar(lado: str, m: dict, token_id: str) -> tuple[float, float, float]:
    """Coloca una orden de compra real en el CLOB al precio ask."""
    usd    = MONTO_FIJO_POR_LADO
    precio = m["best_ask"]

    if usd < MIN_USD_ORDEN:
        log_ev(f"  Orden muy pequeña: ${usd:.2f}")
        return 0.0, 0.0, 0.0

    if usd > estado["capital"]:
        log_ev(f"  Capital insuficiente: ${estado['capital']:.2f} < ${usd:.2f}")
        return 0.0, 0.0, 0.0

    precio = round(precio + 0.01, 4)  # +1¢ para garantizar cruce taker inmediato
    shares = round(usd / precio, 2)

    result = place_taker_buy(token_id, shares, precio)
    if not result["success"]:
        log_ev(f"  ERROR compra {lado}: {result['error']}")
        return 0.0, 0.0, 0.0

    # Usar shares realmente llenadas (puede ser < shares si fue partial fill)
    shares_filled = result.get("shares_filled") or shares
    usd_real      = round(shares_filled * precio, 4)

    estado["capital"] -= usd_real
    log_ev(f"  COMPRA REAL {lado} @ {precio:.4f} (ask) | {shares_filled:.2f}sh (pedido={shares:.2f}) | ${usd_real:.2f} | orderID={result['orderID']}")

    # Pre-aprobar el token condicional para agilizar la venta posterior (patrón basket_prod)
    approve_conditional_token(token_id)

    return precio, shares_filled, usd_real


# ─── SALIDA REAL ──────────────────────────────────────────────────────────────

def forzar_salida(
    shares: float,
    usd_original: float,
    m: dict,
    token_id: str,
    razon: str = "",
) -> tuple[float, float, bool]:
    """
    Vende shares al bid real en el CLOB.
    Retorna (exit_precio, pnl, success).
    Si success=False la posición NO debe limpiarse — se reintentará en el próximo ciclo.
    Usa get_clob_balance para verificar shares reales disponibles (patrón Gold-HARM-ENTRY).
    """
    bid         = m["best_bid"]
    exit_precio = max(round(bid, 4), 0.01)

    # Sincronizar balance del token condicional en el CLOB — hasta 5 intentos
    clob_balance = 0.0
    for _intento_bal in range(5):
        approve_conditional_token(token_id)
        clob_balance = get_clob_balance(token_id)
        if clob_balance >= 0.01:
            break
        time.sleep(1.0)

    shares_a_vender = round(min(clob_balance * 0.99, shares), 2) if clob_balance >= 0.01 else 0.0

    if shares_a_vender <= 0:
        log_ev(f"  VENTA diferida — balance CLOB no disponible ({clob_balance:.4f}) | {razon}")
        return exit_precio, 0.0, False

    result = place_taker_sell(token_id, shares_a_vender, exit_precio)
    if not result["success"]:
        log_ev(f"  ERROR venta @ {exit_precio:.4f} | {result['error']} | {razon}")
        return exit_precio, 0.0, False

    # Usar precio real de fill si está disponible (puede ser mejor que el límite)
    fill_price = result.get("fill_price") or exit_precio
    pnl = round(shares_a_vender * fill_price - usd_original, 4)
    log_ev(f"  VENTA REAL @ {fill_price:.4f} (limite={exit_precio:.4f} bid={bid:.4f}) | {shares_a_vender:.2f}sh | orderID={result['orderID']} | {razon}")
    return fill_price, pnl, True


# ─── SEÑAL DE ENTRADA ─────────────────────────────────────────────────────────

def evaluar_señal(up_m, dn_m):
    obi_up = up_m["obi"]
    obi_dn = dn_m["obi"]
    obi_history_up.append(obi_up)
    obi_history_dn.append(obi_dn)

    signal_up = compute_signal(obi_up, list(obi_history_up), OBI_THRESHOLD)
    signal_dn = compute_signal(obi_dn, list(obi_history_dn), OBI_THRESHOLD)

    if up_m["spread"] > SPREAD_MAX or dn_m["spread"] > SPREAD_MAX:
        return signal_up, signal_dn, None

    mid_up = mid(up_m)
    mid_dn = mid(dn_m)

    if signal_up["combined"] >= OBI_STRONG_THRESHOLD:
        if PRECIO_MIN_LADO1 <= mid_up <= PRECIO_MAX_LADO1:
            return signal_up, signal_dn, "UP"

    if signal_dn["combined"] >= OBI_STRONG_THRESHOLD:
        if PRECIO_MIN_LADO1 <= mid_dn <= PRECIO_MAX_LADO1:
            return signal_up, signal_dn, "DOWN"

    if signal_up["label"] in ("UP", "STRONG UP") and signal_up["combined"] > signal_dn["combined"]:
        if PRECIO_MIN_LADO1 <= mid_up <= PRECIO_MAX_LADO1:
            return signal_up, signal_dn, "UP"

    if signal_dn["label"] in ("UP", "STRONG UP") and signal_dn["combined"] > signal_up["combined"]:
        if PRECIO_MIN_LADO1 <= mid_dn <= PRECIO_MAX_LADO1:
            return signal_up, signal_dn, "DOWN"

    return signal_up, signal_dn, None


# ─── ENTRADA LADO 1 ───────────────────────────────────────────────────────────

def intentar_entrada(up_m, dn_m, mkt, secs) -> bool:
    if pos["activa"]:
        return False
    if secs is None or not (ENTRY_WINDOW_MIN < secs <= ENTRY_WINDOW_MAX):
        return False

    signal_up, signal_dn, lado = evaluar_señal(up_m, dn_m)
    if not lado:
        return False

    m_lado   = up_m if lado == "UP" else dn_m
    token_id = mkt["up_token_id"] if lado == "UP" else mkt["down_token_id"]
    obi      = m_lado["obi"]

    log_ev(f"SEÑAL {lado} — OBI={obi:+.3f} | mid={mid(m_lado):.4f} | {int(secs)}s restantes")

    precio, shares, usd = comprar(lado, m_lado, token_id)
    if usd == 0.0:
        return False

    pos["activa"]          = True
    pos["lado1_side"]      = lado
    pos["lado1_token_id"]  = token_id
    pos["lado1_precio"]    = precio
    pos["lado1_shares"]    = shares
    pos["lado1_usd"]       = usd
    pos["capital_usado"]   = usd
    pos["ts_entrada"]      = time.time()
    pos["secs_entrada"]    = secs or 0

    log_ev(f"ENTRADA LADO1 {lado} @ {precio:.4f} | {shares:.2f}sh | ${usd:.2f} | cap=${estado['capital']:.2f}")
    guardar_estado(up_m, dn_m)
    return True


# ─── HEDGE LADO 2 ─────────────────────────────────────────────────────────────

def intentar_hedge(up_m, dn_m, mkt):
    if not pos["activa"] or pos["hedgeado"]:
        return

    lado1     = pos["lado1_side"]
    lado2     = "DOWN" if lado1 == "UP" else "UP"
    m_lado1   = up_m if lado1 == "UP" else dn_m
    m_lado2   = dn_m if lado2 == "DOWN" else up_m
    token_id2 = mkt["down_token_id"] if lado2 == "DOWN" else mkt["up_token_id"]
    bid_lado1 = m_lado1["best_bid"]
    subida    = bid_lado1 - pos["lado1_precio"]

    if subida < HEDGE_MOVE_MIN:
        return

    obi_lado2 = m_lado2["obi"]
    if obi_lado2 < HEDGE_OBI_MIN:
        return

    ask_lado2 = m_lado2["best_ask"]
    if ask_lado2 <= 0 or ask_lado2 < HEDGE_PRECIO_MIN or ask_lado2 > HEDGE_PRECIO_MAX:
        return

    log_ev(f"  Lado1 subio {subida*100:+.1f}c — hedgeando en {lado2} @ ask={ask_lado2:.4f}")

    precio, shares, usd = comprar(lado2, m_lado2, token_id2)
    if usd == 0.0:
        return

    pos["lado2_side"]      = lado2
    pos["lado2_token_id"]  = token_id2
    pos["lado2_precio"]    = precio
    pos["lado2_shares"]    = shares
    pos["lado2_usd"]       = usd
    pos["hedgeado"]        = True
    pos["capital_usado"]  += usd

    log_ev(f"HEDGE LADO2 {lado2} @ {precio:.4f} | {shares:.2f}sh | ${usd:.2f} | cap=${estado['capital']:.2f}")
    guardar_estado(up_m, dn_m)


# ─── SALIDA ANTICIPADA ────────────────────────────────────────────────────────

def intentar_early_exit(up_m, dn_m, secs):
    if not pos["activa"] or pos["hedgeado"]:
        return

    lado1       = pos["lado1_side"]
    token_id1   = pos["lado1_token_id"]
    m_lado1     = up_m if lado1 == "UP" else dn_m
    bid_lado1   = m_lado1["best_bid"]
    obi_lado1   = m_lado1["obi"]
    secs_en_pos = time.time() - pos["ts_entrada"] if pos["ts_entrada"] else 0
    caida       = pos["lado1_precio"] - bid_lado1

    if secs_en_pos < MIN_HOLD_SECS:
        return

    if bid_lado1 >= NEAR_RESOLUTION_THRESH and secs is not None and secs <= NEAR_RESOLUTION_SECS:
        return

    razon = None
    if secs_en_pos > EARLY_EXIT_SECS:
        razon = f"timeout {int(secs_en_pos)}s sin hedge"
    elif obi_lado1 < EARLY_EXIT_OBI_FLIP:
        razon = f"OBI invertido {obi_lado1:+.3f}"
    elif caida > EARLY_EXIT_PRICE_DROP:
        razon = f"caida {caida*100:.1f}c desde entrada"

    if not razon:
        return

    exit_precio, pnl, ok = forzar_salida(
        pos["lado1_shares"],
        pos["lado1_usd"],
        m_lado1,
        token_id1,
        razon,
    )

    if not ok:
        # Marcar salida pendiente — se reintentará en el próximo ciclo
        pos["salida_pendiente"]  = True
        pos["salida_tipo"]       = "EARLY_EXIT"
        pos["salida_razon"]      = razon
        pos["salida_retries"]    = pos.get("salida_retries", 0) + 1
        log_ev(f"EARLY EXIT pendiente (intento #{pos['salida_retries']}) — reintentando en {POLL_INTERVAL}s")
        return

    estado["capital"]   += pos["lado1_usd"] + pnl
    estado["pnl_total"] += pnl

    if pnl >= 0:
        estado["wins"] += 1
    else:
        estado["losses"] += 1

    actualizar_drawdown()
    log_ev(f"EARLY EXIT {lado1} @ {exit_precio:.4f} | {razon} | PnL: ${pnl:+.4f} | cap=${estado['capital']:.2f}")
    _registrar_trade("EARLY_EXIT", exit_precio, None, "WIN" if pnl >= 0 else "LOSS", pnl)
    resetear_pos()
    guardar_estado(up_m, dn_m)


# ─── RESOLUCIÓN ───────────────────────────────────────────────────────────────

def verificar_resolucion(up_m, dn_m, secs):
    if not pos["activa"]:
        return

    up_mid = mid(up_m)
    dn_mid = mid(dn_m)

    resuelto = None
    if up_mid >= RESOLVED_UP_THRESH:
        resuelto = "UP"
    elif up_mid <= RESOLVED_DN_THRESH:
        resuelto = "DOWN"
    elif dn_mid >= RESOLVED_UP_THRESH:
        resuelto = "DOWN"
    elif secs is not None and secs <= 0:
        resuelto = "UP" if up_mid > 0.5 else "DOWN"
        log_ev(f"Tiempo agotado — resolviendo por mid UP={up_mid:.3f} -> {resuelto}")

    if resuelto:
        _aplicar_resolucion(resuelto, up_m, dn_m)


def _aplicar_resolucion(resuelto: str, up_m, dn_m):
    """
    Vende el lado ganador al bid actual (cerca de 1.0).
    El lado perdedor vale 0 — no se vende, se pierde el capital invertido.
    """
    pnl_total = 0.0
    partes    = []

    # ── Lado 1 ────────────────────────────────────────────────────────────
    if resuelto == pos["lado1_side"]:
        m_win = up_m if pos["lado1_side"] == "UP" else dn_m
        _, pnl_l1, ok1 = forzar_salida(
            pos["lado1_shares"], pos["lado1_usd"], m_win,
            pos["lado1_token_id"], f"resolucion {resuelto}"
        )
        if not ok1:
            # Marcar salida pendiente — se reintentará en el próximo ciclo
            pos["salida_pendiente"]  = True
            pos["salida_tipo"]       = "RESOLUTION"
            pos["salida_resolucion"] = resuelto
            pos["salida_retries"]    = pos.get("salida_retries", 0) + 1
            log_ev(f"RESOLUTION venta pendiente (intento #{pos['salida_retries']}) — reintentando en {POLL_INTERVAL}s")
            return
        partes.append(f"L1 {pos['lado1_side']}=WIN(${pnl_l1:+.2f})")
    else:
        pnl_l1 = -pos["lado1_usd"]
        partes.append(f"L1 {pos['lado1_side']}=LOSS(${pnl_l1:+.2f})")
    pnl_total += pnl_l1

    # ── Lado 2 (hedge) ────────────────────────────────────────────────────
    if pos["hedgeado"]:
        if resuelto == pos["lado2_side"]:
            m_win = dn_m if pos["lado2_side"] == "DOWN" else up_m
            _, pnl_l2, ok2 = forzar_salida(
                pos["lado2_shares"], pos["lado2_usd"], m_win,
                pos["lado2_token_id"], f"resolucion hedge {resuelto}"
            )
            if not ok2:
                pos["salida_pendiente"]  = True
                pos["salida_tipo"]       = "RESOLUTION"
                pos["salida_resolucion"] = resuelto
                pos["salida_retries"]    = pos.get("salida_retries", 0) + 1
                log_ev(f"RESOLUTION hedge venta pendiente (intento #{pos['salida_retries']}) — reintentando en {POLL_INTERVAL}s")
                return
            partes.append(f"L2 {pos['lado2_side']}=WIN(${pnl_l2:+.2f})")
        else:
            pnl_l2 = -pos["lado2_usd"]
            partes.append(f"L2 {pos['lado2_side']}=LOSS(${pnl_l2:+.2f})")
        pnl_total += pnl_l2

    estado["capital"]   += pos["capital_usado"] + pnl_total
    estado["pnl_total"] += pnl_total

    outcome = "WIN" if pnl_total >= 0 else "LOSS"
    if outcome == "WIN":
        estado["wins"] += 1
    else:
        estado["losses"] += 1

    actualizar_drawdown()
    log_ev(
        f"RESOLUCION -> {resuelto} | {' | '.join(partes)} | "
        f"PnL NETO: ${pnl_total:+.2f} | cap=${estado['capital']:.2f}"
    )
    _registrar_trade("RESOLUTION", 1.0 if resuelto == pos["lado1_side"] else 0.0,
                     resuelto, outcome, pnl_total)
    resetear_pos()
    guardar_estado(up_m, dn_m)


def _registrar_trade(tipo, exit_precio, resuelto, outcome, pnl):
    estado["trades"].append({
        "ts":           datetime.now().isoformat(),
        "tipo":         tipo,
        "resolucion":   resuelto,
        "lado1_side":   pos["lado1_side"],
        "lado1_usd":    round(pos["lado1_usd"], 4),
        "lado1_precio": round(pos["lado1_precio"], 4),
        "hedgeado":     pos["hedgeado"],
        "lado2_side":   pos["lado2_side"],
        "lado2_usd":    round(pos["lado2_usd"], 4),
        "lado2_precio": round(pos["lado2_precio"], 4),
        "exit_precio":  round(exit_precio, 4),
        "pnl":          round(pnl, 4),
        "capital":      round(estado["capital"], 4),
        "outcome":      outcome,
    })


# ─── REINTENTOS DE VENTA PENDIENTE ───────────────────────────────────────────

def reintentar_salida_pendiente(up_m, dn_m):
    """
    Reintenta ventas que fallaron en el ciclo anterior.
    Se llama al inicio de cada iteración del loop principal.
    Patrón idéntico al check_stop_loss de Gold-HARM-ENTRY.
    """
    if not pos["activa"] or not pos["salida_pendiente"]:
        return

    pos["salida_retries"] += 1
    log_ev(f"Reintentando venta pendiente (intento #{pos['salida_retries']}) tipo={pos['salida_tipo']}...")

    tipo = pos["salida_tipo"]

    if tipo == "EARLY_EXIT":
        lado1    = pos["lado1_side"]
        token_id = pos["lado1_token_id"]
        m_lado1  = up_m if lado1 == "UP" else dn_m
        razon    = pos["salida_razon"] or "reintento early exit"

        exit_precio, pnl, ok = forzar_salida(
            pos["lado1_shares"], pos["lado1_usd"], m_lado1, token_id, razon
        )
        if not ok:
            return  # sigue pendiente, se reintentará en el próximo ciclo

        pos["salida_pendiente"] = False
        estado["capital"]   += pos["lado1_usd"] + pnl
        estado["pnl_total"] += pnl
        if pnl >= 0: estado["wins"] += 1
        else:        estado["losses"] += 1
        actualizar_drawdown()
        log_ev(f"EARLY EXIT (reintento OK) {lado1} @ {exit_precio:.4f} | PnL: ${pnl:+.4f} | cap=${estado['capital']:.2f}")
        _registrar_trade("EARLY_EXIT", exit_precio, None, "WIN" if pnl >= 0 else "LOSS", pnl)
        resetear_pos()
        guardar_estado(up_m, dn_m)

    elif tipo == "RESOLUTION":
        resuelto = pos["salida_resolucion"]
        _aplicar_resolucion(resuelto, up_m, dn_m)


# ─── CONTROL DEL BOT ──────────────────────────────────────────────────────────

def activar_bot():
    global PAUSED
    PAUSED = False
    log_ev("Bot ACTIVADO")
    guardar_estado()


def pausar_bot():
    global PAUSED
    PAUSED = True
    log_ev("Bot PAUSADO")
    guardar_estado()


# ─── LOOP PRINCIPAL ───────────────────────────────────────────────────────────

async def main_loop():
    global _ob_error_count

    log_ev("=" * 65)
    log_ev("  HEDGE PROD — BTC Up/Down 5m  *** PRODUCCION REAL ***")
    log_ev(f"  Fijo: ${MONTO_FIJO_POR_LADO:.2f}/lado")
    log_ev(f"  Entrada: [{PRECIO_MIN_LADO1:.2f}-{PRECIO_MAX_LADO1:.2f}]")
    log_ev(f"  Hedge: [{HEDGE_PRECIO_MIN:.2f}-{HEDGE_PRECIO_MAX:.2f}] | move_min={HEDGE_MOVE_MIN:.2f}")
    log_ev(f"  MIN_HOLD={MIN_HOLD_SECS}s  NEAR_THRESH={NEAR_RESOLUTION_THRESH}@{NEAR_RESOLUTION_SECS}s")
    log_ev(f"  Bot arranca PAUSADO — usa /api/start para activar")
    log_ev("=" * 65)

    restaurar_estado()
    sincronizar_capital_clob()
    guardar_estado()

    mkt                = None
    loop               = asyncio.get_running_loop()
    signal_up_cache    = None
    signal_dn_cache    = None
    ya_opero_ciclo     = False
    saltar_primer_mkt  = True

    while True:
        try:
            if PAUSED:
                guardar_estado()
                await asyncio.sleep(POLL_INTERVAL * 2)
                continue

            if mkt is None:
                log_ev("Buscando mercado BTC Up/Down 5m...")
                guardar_estado()
                obi_history_up.clear()
                obi_history_dn.clear()
                ya_opero_ciclo = False
                mkt = await loop.run_in_executor(None, find_active_market, "BTC")
                if mkt:
                    if saltar_primer_mkt:
                        log_ev(f"Mercado encontrado (saltado): {mkt.get('question', '')} — siguiente ciclo será el primero")
                        saltar_primer_mkt = False
                        mkt = None
                        await asyncio.sleep(POLL_INTERVAL)
                        continue
                    estado["ciclos"] += 1
                    mkt_end_date = mkt.get("end_date")
                    log_ev(f"Mercado: {mkt.get('question', '')}")
                    # Pre-calentar cache CLOB para ambos tokens — reduce delay de balance al vender
                    approve_conditional_token(mkt["up_token_id"])
                    approve_conditional_token(mkt["down_token_id"])
                    log_ev("  Cache CLOB pre-aprobado (UP + DOWN)")
                    guardar_estado()
                else:
                    log_ev("Sin mercado activo — reintentando en 10s...")
                    guardar_estado()
                    await asyncio.sleep(10)
                    continue

            up_m, err_up = await loop.run_in_executor(
                None, get_order_book_metrics, mkt["up_token_id"]
            )
            dn_m, err_dn = await loop.run_in_executor(
                None, get_order_book_metrics, mkt["down_token_id"]
            )

            if not up_m or not dn_m:
                _ob_error_count += 1
                backoff = min(POLL_INTERVAL * (2 ** _ob_error_count), 60)
                log_ev(f"Error OB #{_ob_error_count}: {err_up or err_dn} — backoff {backoff:.0f}s")
                await asyncio.sleep(backoff)
                continue

            _ob_error_count = 0

            secs = seconds_remaining(mkt)

            if secs is not None and secs <= 0:
                if pos["activa"]:
                    verificar_resolucion(up_m, dn_m, secs)
                log_ev("Mercado expirado — buscando próximo ciclo...")
                mkt = None
                mkt_end_date = None
                await asyncio.sleep(5)
                continue

            # ── Reintentar ventas pendientes del ciclo anterior ───────────
            reintentar_salida_pendiente(up_m, dn_m)

            if pos["activa"] and not pos["salida_pendiente"]:
                verificar_resolucion(up_m, dn_m, secs)

            if pos["activa"] and not pos["hedgeado"] and not pos["salida_pendiente"]:
                intentar_early_exit(up_m, dn_m, secs)

            if pos["activa"] and not pos["hedgeado"] and not pos["salida_pendiente"]:
                intentar_hedge(up_m, dn_m, mkt)

            if not pos["activa"] and not ya_opero_ciclo:
                if intentar_entrada(up_m, dn_m, mkt, secs):
                    ya_opero_ciclo = True

            signal_up_cache = compute_signal(up_m["obi"], list(obi_history_up), OBI_THRESHOLD)
            signal_dn_cache = compute_signal(dn_m["obi"], list(obi_history_dn), OBI_THRESHOLD)

            guardar_estado(up_m, dn_m)
            imprimir_estado(up_m, dn_m, secs, signal_up_cache, signal_dn_cache)

        except Exception as e:
            log_ev(f"Error en loop: {e}")
            import traceback
            traceback.print_exc()

        await asyncio.sleep(POLL_INTERVAL)


# ─── SERVIDOR HTTP ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import csv as csv_module
    import io

    PORT           = int(os.environ.get("PORT", 8080))
    DASHBOARD_FILE = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            try:
                if self.path in ("/", "/index.html"):
                    self._serve_dashboard()
                elif self.path == "/api/status":
                    self._serve_status()
                elif self.path == "/api/trades":
                    self._serve_trades()
                elif self.path == "/api/csv":
                    self._serve_csv()
                elif self.path == "/api/events":
                    self._serve_events()
                else:
                    self._send(404, "text/plain", b"Not found")
            except Exception as e:
                self._send(500, "text/plain", str(e).encode())

        def do_POST(self):
            if self.path == "/api/start":
                activar_bot()
                self._send(200, "application/json", b'{"ok":true,"msg":"Bot activado"}')
            elif self.path == "/api/stop":
                pausar_bot()
                self._send(200, "application/json", b'{"ok":true,"msg":"Bot pausado"}')
            elif self.path == "/api/reset":
                resetear_pos()
                log_ev("Posicion reseteada manualmente via /api/reset")
                guardar_estado()
                self._send(200, "application/json", b'{"ok":true,"msg":"Posicion reseteada"}')
            else:
                self._send(404, "text/plain", b"Not found")

        def _serve_dashboard(self):
            if os.path.isfile(DASHBOARD_FILE):
                with open(DASHBOARD_FILE, "rb") as f:
                    body = f.read()
                self._send(200, "text/html; charset=utf-8", body)
            else:
                try:
                    if os.path.isfile(STATE_FILE):
                        with open(STATE_FILE) as f:
                            st = json.load(f)
                    else:
                        st = {}
                    body = (
                        f"<html><body><pre>HEDGE PROD — PRODUCCION REAL\n"
                        f"Capital: ${st.get('capital', CAPITAL_INICIAL):.2f}\n"
                        f"PnL: ${st.get('pnl_total', 0):.2f}\n"
                        f"Paused: {st.get('paused', True)}\n"
                        f"W:{st.get('wins',0)} L:{st.get('losses',0)}</pre></body></html>"
                    ).encode()
                except Exception:
                    body = b"<html><body>HEDGE PROD - OK</body></html>"
                self._send(200, "text/html; charset=utf-8", body)

        def _serve_status(self):
            try:
                if os.path.isfile(STATE_FILE):
                    with open(STATE_FILE) as f:
                        data = json.load(f)
                else:
                    data = {
                        "capital": CAPITAL_INICIAL, "capital_inicial": CAPITAL_INICIAL,
                        "pnl_total": 0, "roi": 0, "win_rate": 0, "wins": 0, "losses": 0,
                        "max_drawdown": 0, "ciclos": 0, "paused": PAUSED,
                        "posicion": {"activa": False},
                        "eventos": [], "trades": [], "ts": datetime.now().isoformat(),
                    }
                data["capital_inicial"] = data.get("capital_inicial", CAPITAL_INICIAL)
                self._send(200, "application/json", json.dumps(data).encode())
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode())

        def _serve_trades(self):
            try:
                trades = []
                if os.path.isfile(LOG_FILE):
                    with open(LOG_FILE) as f:
                        trades = json.load(f).get("trades", [])
                self._send(200, "application/json", json.dumps(trades).encode())
            except Exception:
                self._send(500, "application/json", b"[]")

        def _serve_csv(self):
            try:
                trades = []
                if os.path.isfile(LOG_FILE):
                    with open(LOG_FILE) as f:
                        trades = json.load(f).get("trades", [])
                if not trades:
                    self._send(200, "text/csv", b"sin trades")
                    return
                buf    = io.StringIO()
                writer = csv_module.DictWriter(buf, fieldnames=trades[0].keys())
                writer.writeheader()
                writer.writerows(trades)
                body = buf.getvalue().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", "attachment; filename=trades.csv")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._send(500, "text/plain", str(e).encode())

        def _serve_events(self):
            try:
                lines = []
                if os.path.isfile(EVENTS_FILE):
                    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                        lines = f.readlines()[-100:]
                body = "".join(lines).encode("utf-8")
                self._send(200, "text/plain; charset=utf-8", body)
            except Exception as e:
                self._send(500, "text/plain", str(e).encode())

        def _send(self, code, ctype, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def run_http():
        srv = HTTPServer(("0.0.0.0", PORT), Handler)
        log.info(f"Dashboard en http://0.0.0.0:{PORT}")
        srv.serve_forever()

    threading.Thread(target=run_http, daemon=True).start()

    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        log.info("Bot detenido.")
