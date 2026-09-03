"""
OANDA Flip Bot  -  main loop
----------------------------
Strategy ("hedge-and-flip"):
  * Keep BOTH a BUY ("long") and a SELL ("short") open on the same instrument.
  * On each poll, whichever side price has moved in favour of is the "winner";
    the opposite side is the "loser".
  * When the losing side has lost MIN_POSITION_SHIFT pips, we FLIP:
      - close the losing side,
      - open a fresh order on that same side AT THE CURRENT MARKET PRICE.
    This re-centres the grid and follows the trend while keeping a thin hedge.
  * Stop when cumulative P&L since start hits PROFIT_TARGET (victory) or
    <= -MAX_LOSS (the "$1 test" cap), or when you press Ctrl-C.

Usage:
    export OANDA_API_TOKEN=... OANDA_ACCOUNT_ID=...
    python bot.py          # or: python3 bot.py
"""

import json
import logging
import os
import sys
import time
from datetime import datetime

from config import config
from oanda_client import OandaClient


# --------------------------------------------------------------------------- #
#  Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("flipbot")

FLIPS = []  # history of flips for the dashboard


def write_state(**kv):
    """Persist current results to STATE_FILE so the web dashboard can read it."""
    data = {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "instrument": config.INSTRUMENT,
        "mode": config.ENTRY_MODE,
        "running": True,
        "starting_balance": config.STARTING_BALANCE,
        "target": config.PROFIT_TARGET,
        "max_loss": config.MAX_LOSS,
        "flips": FLIPS[-40:],
        **kv,
    }
    try:
        with open(config.STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"could not write state: {e}")


def pnl_in_pips(client, instrument, trade_side, entry_price, pip):
    """Approximate open P&L of one leg in pips (signed)."""
    px = client.price(instrument)
    if trade_side == "long":
        return float((px["bid"] - entry_price) / pip)
    return float((entry_price - px["ask"]) / pip)


def get_legs(client, instrument):
    """Return (legs) for the instrument, built from OPEN TRADES so a fully
    hedged position (net = 0) is still visible. Returns a dict with 'long' and
    'short' legs, or None if no trades exist on the instrument."""
    trades = client.open_trades()
    inst_trades = [t for t in trades if t["instrument"] == instrument]
    if not inst_trades:
        return None

    longs = [t for t in inst_trades if int(t["currentUnits"]) > 0]
    shorts = [t for t in inst_trades if int(t["currentUnits"]) < 0]

    def agg(legs_list):
        if not legs_list:
            return {"units": 0, "pl": 0.0, "entry": 0.0}
        units = sum(int(t["currentUnits"]) for t in legs_list)
        # volume-weighted average entry price
        vol = sum(abs(int(t["currentUnits"])) for t in legs_list)
        entry = sum(float(t["price"]) * abs(int(t["currentUnits"])) for t in legs_list) / vol if vol else 0.0
        pl = sum(float(t["unrealizedPL"]) for t in legs_list)
        return {"units": units, "pl": pl, "entry": entry}

    return {"long": agg(longs), "short": agg(shorts)}


def main():
    if not config.API_TOKEN or not config.ACCOUNT_ID:
        log.error("Missing OANDA_API_TOKEN or OANDA_ACCOUNT_ID. See README.md / config.py.")
        sys.exit(1)

    client = OandaClient(config.API_TOKEN, config.ACCOUNT_ID, config.base_url)
    pip = config.ticks_to_pips
    instr = config.INSTRUMENT

    # --- baseline: net equity (balance + unrealized) at start ---------------
    summary = client.account_summary()
    start_equity = float(summary["balance"]) + float(summary["unrealizedPL"])
    log.info(f"Connected. Account={config.ACCOUNT_ID}  starting net equity=${start_equity:.4f}")
    log.info(f"Instrument={instr}  mode={config.ENTRY_MODE}  offset={config.OFFSET_PIPS}p "
             f"units={config.UNITS}/leg  target=${config.PROFIT_TARGET}  maxloss=${config.MAX_LOSS}")

    px = client.price(instr)
    log.info(f"Initial price: bid={px['bid']:.5f} ask={px['ask']:.5f}")

    # --- seed the entry ----------------------------------------------------
    if config.ENTRY_MODE == "hedge":
        # NOTE: requires a HEDGING account; on a netting account the two
        # opposite orders cancel to zero. See README.
        client.market_order(instr, +config.UNITS)
        client.market_order(instr, -config.UNITS)
        log.info(f"Opened hedge: BUY {config.UNITS} + SELL {config.UNITS} "
                 f"(requires hedging account)")
        _cancel_pending(client, instr)  # clear any leftover pending orders
    elif config.ENTRY_MODE == "straddle":
        # Breakout entry: BUY STOP above, SELL STOP below. One will fill.
        _cancel_pending(client, instr)
        up = round(px["mid"] + config.OFFSET_PIPS * pip, 5)
        dn = round(px["mid"] - config.OFFSET_PIPS * pip, 5)
        client.stop_order(instr, +config.UNITS, str(up), "STOP")
        client.stop_order(instr, -config.UNITS, str(dn), "STOP")
        log.info(f"Placed breakout straddle: BUY STOP @{up} / SELL STOP @{dn}. "
                 f"Waiting for breakout...")
    else:
        log.error(f"Unknown ENTRY_MODE {config.ENTRY_MODE}")
        sys.exit(1)

    iterations = 0
    peak_pnl = 0.0  # highest cumulative profit reached (drives the growing risk budget)
    log.info("Running. Ctrl-C to stop.")
    try:
        while True:
            iterations += 1
            summary = client.account_summary()
            balance = float(summary["balance"])
            unrealized = float(summary["unrealizedPL"])
            equity = balance + unrealized
            pnl = equity - start_equity
            pl_pct = (pnl / config.STARTING_BALANCE) * 100.0 if config.STARTING_BALANCE else 0

            px = client.price(instr)

            # Compounding risk budget: $1 + peak profit, capped at MAX_LOSS.
            peak_pnl = max(peak_pnl, pnl)
            risk_budget = min(config.STARTING_BALANCE + max(peak_pnl, 0.0), config.MAX_LOSS)

            # --- cumulative stop conditions ---------------------------------
            if pnl >= config.PROFIT_TARGET:
                log.info(f"PROFIT TARGET HIT: banked ${pnl:.4f} (peak ${peak_pnl:.4f}). Closing and stopping.")
                _close_all(client, instr)
                write_state(running=False, reason="profit-target", exit_pnl=round(pnl, 4))
                break
            if pnl <= -risk_budget:
                log.info(f"LOSS CAP HIT: net P&L ${pnl:.4f} reached the growing "
                         f"risk budget (-${risk_budget:.2f}, peak profit ${peak_pnl:.2f}). Closing and stopping.")
                _close_all(client, instr)
                write_state(running=False, reason="loss-cap", exit_pnl=round(pnl, 4))
                break
            if config.STOP_AFTER_ITERATIONS and iterations >= config.STOP_AFTER_ITERATIONS:
                log.info(f"Reached STOP_AFTER_ITERATIONS={config.STOP_AFTER_ITERATIONS}. Stopping.")
                _close_all(client, instr)
                write_state(running=False, reason="max-iterations", exit_pnl=round(pnl, 4))
                break

            if config.ENTRY_MODE == "straddle":
                _run_straddle_tick(client, instr, px, pip)
            else:
                _run_hedge_tick(client, instr, px, pip)

            # Update legs state for the dashboard.
            legs = get_legs(client, instr)
            legs_state = None
            if legs and (abs(legs["long"]["units"]) or abs(legs["short"]["units"])):
                legs_state = {
                    "long": {"pl": legs["long"]["pl"], "entry": legs["long"]["entry"]},
                    "short": {"pl": legs["short"]["pl"], "entry": legs["short"]["entry"]},
                }
            write_state(
                price={"bid": px["bid"], "ask": px["ask"], "mid": px["mid"]},
                equity=round(equity, 4),
                pnl=round(pnl, 4),
                pnl_pct=round(pl_pct, 2),
                peak_pnl=round(peak_pnl, 4),
                risk_budget=round(risk_budget, 4),
                target=config.PROFIT_TARGET,
                max_loss=config.MAX_LOSS,
                iterations=iterations,
                legs=legs_state,
            )

            # Brief log line.
            legs_txt = "no open position"
            if legs_state:
                legs_txt = (f"long_pl={legs_state['long']['pl']:+.2f} "
                            f"short_pl={legs_state['short']['pl']:+.2f}")
            log.info(f"[{iterations}] mid={px['mid']:.5f} equity=${equity:.4f} "
                     f"P&L=${pnl:+.4f} ({pl_pct:+.2f}%) | budget(risk)=${risk_budget:.2f} "
                     f"peak=${peak_pnl:.2f} | {legs_txt}")

            time.sleep(config.POLL_INTERVAL)

    except KeyboardInterrupt:
        log.info("Stopped by user. Closing positions.")
        try:
            _cancel_pending(client, instr)
            _close_all(client, instr)
        except Exception as e:
            log.error(f"Could not close: {e}")
        write_state(running=False, reason="user-stop")
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        try:
            _cancel_pending(client, instr)
            _close_all(client, instr)
        except Exception:
            pass
        sys.exit(1)


def _cancel_pending(client, instrument):
    """Cancel any open (unfilled) pending STOP orders on the instrument."""
    try:
        orders = client._request("GET", "/pendingOrders").get("orders", [])
        for o in orders:
            if o.get("instrument") == instrument:
                client.cancel_order(o["id"])
                log.info(f"Cancelled pending order {o['id']} ({o['type']} @ {o.get('price')})")
    except Exception as e:
        log.warning(f"_cancel_pending error: {e}")


def _run_straddle_tick(client, instr, px, pip):
    """Breakout / single-direction flip logic (works on netting accounts).

    After a breakout fills one side, cancel the other. Then, if the open
    position starts LOSING past MIN_POSITION_SHIFT pips, close it and flip to
    the opposite direction at market (follow the move)."""
    trades = [t for t in client.open_trades() if t["instrument"] == instr]
    if not trades:
        return  # still waiting for a breakout to fill a pending order

    # Take the open trade (only one on a netting account).
    t = trades[0]
    units = int(t["currentUnits"])
    entry = float(t["price"])
    side = "long" if units > 0 else "short"
    pips = (px["bid"] - entry) / pip if side == "long" else (entry - px["ask"]) / pip

    if pips <= -config.MIN_POSITION_SHIFT:
        # Flip: close current losing position, open opposite at market.
        log.info(f"  FLIP: {side} is losing ({pips:+.1f}p @ {px['mid']:.5f}). "
                 f"Closing and going opposite at market.")
        if side == "long":
            client.close_position(instr, "long", "ALL")
            client.market_order(instr, -config.UNITS)
        else:
            client.close_position(instr, "short", "ALL")
            client.market_order(instr, +config.UNITS)
        FLIPS.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "side": "short" if side == "long" else "long",
            "price": px["mid"],
            "pips": round(pips, 1),
        })


def _run_hedge_tick(client, instr, px, pip):
    """Hedge-and-flip logic (requires a HEDGING account)."""
    legs = get_legs(client, instr)
    if not legs:
        return
    long_units = abs(legs["long"]["units"])
    short_units = abs(legs["short"]["units"])
    long_pips = pnl_in_pips(client, instr, "long", legs["long"]["entry"], pip) if long_units else 0.0
    short_pips = pnl_in_pips(client, instr, "short", legs["short"]["entry"], pip) if short_units else 0.0

    candidates = []
    if long_units:
        candidates.append(("long", long_pips))
    if short_units:
        candidates.append(("short", short_pips))
    if not candidates:
        return

    if len(candidates) == 2:
        losing_side, losing_pips = min(candidates, key=lambda c: c[1])
    else:
        losing_side, losing_pips = candidates[0]

    if losing_pips <= -config.MIN_POSITION_SHIFT:
        log.info(f"  FLIP: {losing_side} is losing ({losing_pips:+.1f}p). "
                 f"Closing losing {losing_side} and re-opening it at market.")
        client.close_position(instr, "long" if losing_side == "long" else "short")
        if losing_side == "long":
            client.market_order(instr, +config.UNITS)
        else:
            client.market_order(instr, -config.UNITS)
        FLIPS.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "side": losing_side,
            "price": px["mid"],
            "pips": round(losing_pips, 1),
        })


def _close_all(client, instrument):
    """Safely flatten any open position on the instrument, using OPEN TRADES
    (the net position endpoint hides a full hedge)."""
    try:
        trades = client.open_trades()
        inst_trades = [t for t in trades if t["instrument"] == instrument]
        if not inst_trades:
            log.info("No open position to close.")
            return
        longs = sum(int(t["currentUnits"]) for t in inst_trades if int(t["currentUnits"]) > 0)
        shorts = sum(int(t["currentUnits"]) for t in inst_trades if int(t["currentUnits"]) < 0)
        if longs:
            client.close_position(instrument, "long", "ALL")
            log.info("Closed long leg.")
        if shorts:
            client.close_position(instrument, "short", "ALL")
            log.info("Closed short leg.")
    except Exception as e:
        log.error(f"close_all error: {e}")


if __name__ == "__main__":
    main()
