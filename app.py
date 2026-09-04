"""
OANDA Flip Bot  -  Web dashboard
--------------------------------
Serves a live dashboard showing candlesticks, the current price, the bot's
results (P&L, equity, positions, flips), and Start/Stop controls.

Two data modes:
  * LIVE  - reads a real OANDA demo account + candles when OANDA_API_TOKEN /
            OANDA_ACCOUNT_ID are set in the environment / .env.
  * DEMO  - simulated candles + simulated results, so the UI can be previewed
            with no token. Enabled automatically when no token is present
            (or when SIM=1).

Run:      python app.py
Open:     http://127.0.0.1:5000   (or the live preview URL)
"""

import json
import os
import random
import subprocess
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from config import config

# --------------------------------------------------------------------------- #
#  Demo / simulated data (used when no real token is configured)
# --------------------------------------------------------------------------- #
SIM_START = 1.08400
SIM_PRICE = [SIM_START]
SIM_OHLC = []  # list of dicts: {t,o,h,l,c}


def sim_tick():
    drift = random.gauss(0.000005, 0.00012)
    SIM_PRICE[0] += drift
    return SIM_PRICE[0]


def build_sim_candles(n=120, step="M1"):
    """Generate/refresh a plausible candle series as a random walk."""
    now = time.time()
    candles = []
    price = SIM_PRICE[0]
    # back-fill history
    history = []
    for i in range(n):
        p = price
        # random walk a bit
        for _ in range(4):
            p += random.gauss(0, 0.00006)
        o, high, low, close = price, max(price, p), min(price, p), p
        history.append({"t": now - (n - i) * 60, "o": o, "h": high, "l": low, "c": close})
        price = p
    SIM_PRICE[0] = price
    return [{"time": int(c["t"]), "open": round(c["o"], 5), "high": round(c["h"], 5),
             "low": round(c["l"], 5), "close": round(c["c"], 5)} for c in history]


def sim_state():
    pnl = sum(random.gauss(0.0, 0.02) for _ in range(6))
    return {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "instrument": config.INSTRUMENT,
        "mode": config.ENTRY_MODE,
        "running": "sim",
        "starting_balance": config.STARTING_BALANCE,
        "target": config.PROFIT_TARGET,
        "max_loss": config.MAX_LOSS,
        "equity": round(config.STARTING_BALANCE + pnl, 4),
        "pnl": round(pnl, 4),
        "pnl_pct": round(pnl / config.STARTING_BALANCE * 100, 2) if config.STARTING_BALANCE else 0,
        "iterations": 0,
        "price": {"bid": round(SIM_PRICE[0] - 0.00008, 5), "ask": round(SIM_PRICE[0] + 0.00008, 5),
                  "mid": round(SIM_PRICE[0], 5)},
        "legs": None,
        "flips": [],
        "reason": None,
    }


# --------------------------------------------------------------------------- #
#  OANDA access (live) or None in demo mode
# --------------------------------------------------------------------------- #
client = None
LIVE = bool(config.API_TOKEN and config.ACCOUNT_ID)
if LIVE:
    from oanda_client import OandaClient
    client = OandaClient(config.API_TOKEN, config.ACCOUNT_ID, config.base_url)

app = Flask(__name__)


def get_candles(n=120):
    if client:
        try:
            raws = client.candles(config.INSTRUMENT, count=n, granularity="M1")
            out = []
            for c in raws:
                mid = c["mid"]
                out.append({"time": int(c["time"].replace("-", "")[:13] + "000"),
                            "open": float(mid["o"]), "high": float(mid["h"]),
                            "low": float(mid["l"]), "close": float(mid["c"])})
            return out
        except Exception as e:
            return {"error": str(e)}
    return build_sim_candles(n)


def current_price():
    if client:
        try:
            p = client.price(config.INSTRUMENT)
            return {"bid": p["bid"], "ask": p["ask"], "mid": p["mid"]}
        except Exception:
            pass
    sim_tick()
    return {"bid": round(SIM_PRICE[0] - 0.00008, 5), "ask": round(SIM_PRICE[0] + 0.00008, 5),
            "mid": round(SIM_PRICE[0], 5)}


def read_state():
    p = Path(config.STATE_FILE)
    if p.exists() and p.stat().st_size:
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    if not LIVE:
        return sim_state()
    # LIVE mode, no fresh state file (e.g. bot runs in a separate worker
    # service on Render). Build a snapshot straight from OANDA.
    try:
        return live_oanda_state()
    except Exception:
        pass
    return {"running": False, "reason": None, "instrument": config.INSTRUMENT,
            "message": "Bot not running yet."}


def live_oanda_state():
    """Build a live dashboard snapshot directly from OANDA (works when the bot
    process and the web process are separate instances)."""
    summary = client.account_summary()
    equity = float(summary["balance"]) + float(summary["unrealizedPL"])
    px = client.price(config.INSTRUMENT)
    trades = client.open_trades()
    inst = [t for t in trades if t["instrument"] == config.INSTRUMENT]
    longs = [t for t in inst if int(t["currentUnits"]) > 0]
    shorts = [t for t in inst if int(t["currentUnits"]) < 0]
    legs = None
    if inst:
        legs = {
            "long": {"pl": sum(float(t["unrealizedPL"]) for t in longs),
                     "entry": float(longs[0]["price"]) if longs else 0.0},
            "short": {"pl": sum(float(t["unrealizedPL"]) for t in shorts),
                      "entry": float(shorts[0]["price"]) if shorts else 0.0},
        }
    return {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "instrument": config.INSTRUMENT,
        "mode": config.ENTRY_MODE,
        "running": bool(inst),
        "starting_balance": config.STARTING_BALANCE,
        "target": config.PROFIT_TARGET,
        "max_loss": config.MAX_LOSS,
        "equity": round(equity, 4),
        "pnl": round(float(summary["unrealizedPL"]), 4),
        "pnl_pct": round(float(summary["unrealizedPL"]) / config.STARTING_BALANCE * 100, 2)
        if config.STARTING_BALANCE else 0,
        "iterations": 0,
        "price": {"bid": px["bid"], "ask": px["ask"], "mid": px["mid"]},
        "legs": legs,
        "flips": [],
        "reason": None,
        "source": "oanda-live",
    }


# --------------------------------------------------------------------------- #
#  Bot Start/Stop control (writes control.json, which bot.py + the supervisor
#  in start.sh read). Works regardless of gunicorn workers since it only
#  touches a shared file. In demo/sim mode we just echo a message.
# --------------------------------------------------------------------------- #
def write_control(**kv):
    data = {}
    try:
        with open(config.CONTROL_FILE) as f:
            data = json.load(f)
    except Exception:
        pass
    data.update(kv)
    try:
        with open(config.CONTROL_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        return {"ok": False, "message": f"could not write control: {e}"}
    return {"ok": True, "data": data}


@app.route("/api/start", methods=["POST"])
def start_bot():
    if not LIVE:
        return jsonify({"ok": True, "mode": "sim", "message": "Demo mode - simulated."})
    write_control(stop=False)
    return jsonify({"ok": True, "message": "Bot STARTED. Trader running."})


@app.route("/api/stop", methods=["POST"])
def stop_bot():
    if not LIVE:
        return jsonify({"ok": True, "mode": "sim", "message": "Demo mode - simulated."})
    write_control(stop=True)
    return jsonify({"ok": True, "message": "Stop requested. Closing positions and halting the trader."})


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html", instrument=config.INSTRUMENT,
                           demo=LIVE is None, live=LIVE)


@app.route("/api/candles")
def api_candles():
    n = int(request.args.get("n", 120))
    return jsonify(get_candles(n))


@app.route("/api/price")
def api_price():
    return jsonify(current_price())


@app.route("/api/state")
def api_state():
    return jsonify(read_state())


@app.route("/api/config")
def api_config():
    return jsonify({
        "instrument": config.INSTRUMENT,
        "mode": config.ENTRY_MODE,
        "units": config.UNITS,
        "offset_pips": config.OFFSET_PIPS,
        "start_balance": config.STARTING_BALANCE,
        "target": config.PROFIT_TARGET,
        "max_loss": config.MAX_LOSS,
        "poll_interval": config.POLL_INTERVAL,
        "min_shift": config.MIN_POSITION_SHIFT,
        "live": LIVE,
    })


@app.route("/api/logs")
def api_logs():
    """Return the tail of the bot log (flip_bot.log) for the dashboard Logs panel."""
    n = int(request.args.get("n", 200))
    path = config.LOG_FILE
    lines = []
    if os.path.exists(path):
        try:
            with open(path, "r", errors="replace") as f:
                all_lines = f.readlines()
            lines = [ln.rstrip("\n") for ln in all_lines[-n:]]
        except Exception as e:
            lines = [f"[log read error] {e}"]
    else:
        lines = [f"[log file not found: {path}]"]
    return jsonify({"log": lines, "path": path})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n  OANDA Flip Bot Dashboard")
    print(f"  mode: {'LIVE' if LIVE else 'DEMO (simulated)'}  instrument: {config.INSTRUMENT}")
    print(f"  open:  http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
