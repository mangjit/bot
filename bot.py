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
    """Return (long_pl, short_pl, long_pips, short_pips, entries) or None."""
    pos = client.position(instrument)
    if not pos:
        return None
    long_pl = float(pos["long"]["unrealizedPL"]) if pos["long"]["units"] != "0" else 0.0
    short_pl = float(pos["short"]["unrealizedPL"]) if pos["short"]["units"] != "0" else 0.0
    return {
        "long": {"units": int(pos["long"]["units"]), "pl": long_pl,
                 "entry": float(pos["long"]["averagePrice"] or 0)},
        "short": {"units": int(pos["short"]["units"]), "pl": short_pl,
                  "entry": float(pos["short"]["averagePrice"] or 0)},
    }


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

    # --- seed the two legs --------------------------------------------------
    if config.ENTRY_MODE == "hedge":
        # Open both at market (hedged). Real offset is applied on re-arm only.
        client.market_order(instr, +config.UNITS)
        client.market_order(instr, -config.UNITS)
        log.info(f"Opened hedge: BUY {config.UNITS} + SELL {config.UNITS}")
    elif config.ENTRY_MODE == "straddle":
        up = round(px["mid"] + config.OFFSET_PIPS * pip, 5)
        dn = round(px["mid"] - config.OFFSET_PIPS * pip, 5)
        client.stop_order(instr, +config.UNITS, str(up), "STOP")
        client.stop_order(instr, -config.UNITS, str(dn), "STOP")
        log.info(f"Placed straddle: BUY STOP @{up} / SELL STOP @{dn}")
    else:
        log.error(f"Unknown ENTRY_MODE {config.ENTRY_MODE}")
        sys.exit(1)

    iterations = 0
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
            legs = get_legs(client, instr)

            # --- stop conditions -------------------------------------------
            if pnl >= config.PROFIT_TARGET:
                log.info(f"TARGET HIT: net P&L ${pnl:.4f} (+{pl_pct:.2f}% of ${config.STARTING_BALANCE}). Closing and stopping.")
                _close_all(client, instr)
                write_state(running=False, reason="target-hit", exit_pnl=round(pnl, 4))
                break
            if pnl <= -config.MAX_LOSS:
                log.info(f"LOSS CAP HIT: net P&L ${pnl:.4f} ({pl_pct:.2f}%). Closing and stopping.")
                _close_all(client, instr)
                write_state(running=False, reason="loss-cap", exit_pnl=round(pnl, 4))
                break
            if config.STOP_AFTER_ITERATIONS and iterations >= config.STOP_AFTER_ITERATIONS:
                log.info(f"Reached STOP_AFTER_ITERATIONS={config.STOP_AFTER_ITERATIONS}. Stopping.")
                _close_all(client, instr)
                write_state(running=False, reason="max-iterations", exit_pnl=round(pnl, 4))
                break

            head = (f"[{iterations}] mid={px['mid']:.5f} equity=${equity:.4f} "
                    f"P&L=${pnl:+.4f} ({pl_pct:+.2f}%) | "
                    f"long_pl={legs['long']['pl']:+.2f} short_pl={legs['short']['pl']:+.2f} "
                    if legs else f"[{iterations}] mid={px['mid']:.5f} equity=${equity:.4f} P&L=${pnl:+.4f} | no net position ")

            # --- flip decision (hedge mode) ---------------------------------
            if legs and config.ENTRY_MODE == "hedge":
                long_pips = pnl_in_pips(client, instr, "long", legs["long"]["entry"], pip)
                short_pips = pnl_in_pips(client, instr, "short", legs["short"]["entry"], pip)
                head += f"long_pips={long_pips:+.1f} short_pips={short_pips:+.1f}"

                # The losing side is the one whose pip P&L is more negative.
                losing_side = "short" if long_pips >= short_pips else "long"
                losing_pips = short_pips if losing_side == "short" else long_pips

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
                    head += "  ^FLIPPED"

            # Write results for the web dashboard.
            legs_state = None
            if legs:
                legs_state = {
                    "long": {"pl": legs["long"]["pl"], "entry": legs["long"]["entry"]},
                    "short": {"pl": legs["short"]["pl"], "entry": legs["short"]["entry"]},
                }
            write_state(
                price={"bid": px["bid"], "ask": px["ask"], "mid": px["mid"]},
                equity=round(equity, 4),
                pnl=round(pnl, 4),
                pnl_pct=round(pl_pct, 2),
                iterations=iterations,
                legs=legs_state,
            )

            log.info(head if config.VERBOSE else head.split("|")[0])
            time.sleep(config.POLL_INTERVAL)

    except KeyboardInterrupt:
        log.info("Stopped by user. Closing positions.")
        try:
            _close_all(client, instr)
        except Exception as e:
            log.error(f"Could not close: {e}")
        write_state(running=False, reason="user-stop")
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        try:
            _close_all(client, instr)
        except Exception:
            pass
        sys.exit(1)


def _close_all(client, instrument):
    """Safely flatten any open position on the instrument."""
    try:
        pos = client.position(instrument)
        if pos:
            if pos["long"]["units"] != "0":
                client.close_position(instrument, "long", "ALL")
                log.info("Closed long leg.")
            if pos["short"]["units"] != "0":
                client.close_position(instrument, "short", "ALL")
                log.info("Closed short leg.")
        else:
            log.info("No open position to close.")
    except Exception as e:
        log.error(f"close_all error: {e}")


if __name__ == "__main__":
    main()
