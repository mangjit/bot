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

from config import config, reset_runtime, SETTINGS_FILE

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


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    """Return current API/settings (token masked)."""
    tk = config.API_TOKEN
    masked = (tk[:4] + "…" + tk[-4:]) if len(tk) > 8 else ("SET" if tk else "")
    return jsonify({
        "api_token_set": bool(tk),
        "api_token_masked": masked,
        "account_id": config.ACCOUNT_ID,
        "env": config.ENV,
        "instrument": config.INSTRUMENT,
        "entry_mode": config.ENTRY_MODE,
        "units": config.UNITS,
        "offset_pips": config.OFFSET_PIPS,
        "starting_balance": config.STARTING_BALANCE,
        "max_loss": config.MAX_LOSS,
        "profit_target": config.PROFIT_TARGET,
        "poll_interval": config.POLL_INTERVAL,
        "min_shift": config.MIN_POSITION_SHIFT,
        "live": LIVE,
        "settings_file": SETTINGS_FILE,
    })


@app.route("/api/settings", methods=["POST"])
def api_post_settings():
    """Save API settings to settings.json so bot.py + app.py pick them up."""
    data = request.get_json(force=True) or {}
    allowed = {"API_TOKEN", "ACCOUNT_ID", "ENV", "INSTRUMENT", "ENTRY_MODE",
               "UNITS", "OFFSET_PIPS", "STARTING_BALANCE", "MAX_LOSS",
               "PROFIT_TARGET", "POLL_INTERVAL", "MIN_POSITION_SHIFT",
               "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"}
    # Load existing so we don't clobber unmentioned keys.
    try:
        with open(SETTINGS_FILE) as f:
            existing = json.load(f)
    except Exception:
        existing = {}
    tok_changed = False
    for k in allowed:
        if k in data and data[k] is not None:
            # If API_TOKEN is the masked placeholder, keep the original.
            if k == "API_TOKEN":
                v = str(data[k]).strip()
                if not v or v.startswith("…") or (config.API_TOKEN and v == (config.API_TOKEN[:4] + "…" + config.API_TOKEN[-4:])):
                    tok_changed = False
                    continue
                tok_changed = True
                existing["API_TOKEN"] = v
            else:
                existing[k] = data[k]
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        return jsonify({"ok": False, "message": f"could not write settings: {e}"}), 500
    reset_runtime()
    return jsonify({"ok": True, "message": "Settings saved. Restart the bot (Start/Stop) to apply connection changes.",
                    "token_changed": tok_changed})


@app.route("/api/test", methods=["POST"])
def api_test():
    """Test the OANDA connection with the CURRENT creds."""
    if not (config.API_TOKEN and config.ACCOUNT_ID):
        return jsonify({"ok": False, "message": "No API token / account ID set."})
    try:
        c = OandaClient(config.API_TOKEN, config.ACCOUNT_ID, config.base_url)
        s = c.account_summary()
        price = c.price(config.INSTRUMENT)
        return jsonify({"ok": True,
                        "message": f"Connected to {config.ENV} account {config.ACCOUNT_ID}. "
                                   f"Balance ${float(s.get('balance',0)):,.2f}, "
                                   f"{config.INSTRUMENT} mid={price['mid']:.5f}"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Connection failed: {e}"})


# --------------------------------------------------------------------------- #
#  Cloud LLM chat (OpenAI-compatible). Works with any base URL + API key.
# --------------------------------------------------------------------------- #
import requests as _requests


def _norm_url(base):
    """Normalise a base URL: add missing scheme, trailing /v1, and strip slashes."""
    b = (base or "").strip().rstrip("/")
    if not b:
        b = "https://api.openai.com/v1"
    if not b.startswith("http"):
        b = "https://" + b
    # Ensure it points at the versioned root for /models and /chat/completions.
    if not b.endswith("/v1") and "/v1/" not in b and b.split("://")[-1].count("/") <= 1:
        b = b + "/v1"
    return b


def _llm_headers(api_key):
    h = {"Content-Type": "application/json"}
    if api_key:
        h["Authorization"] = "Bearer " + api_key
    return h


def _llm_models(base, api_key):
    """Fetch the model list from an OpenAI-compatible endpoint."""
    url = _norm_url(base) + "/models"
    r = _requests.get(url, headers=_llm_headers(api_key), timeout=30)
    r.raise_for_status()
    data = r.json()
    ids = []
    # Standard: {"data":[{"id":...}]}. Some proxies vary; be lenient.
    if isinstance(data, dict) and "data" in data:
        ids = [m.get("id") for m in data["data"] if m.get("id")]
    elif isinstance(data, list):
        ids = [m.get("id") if isinstance(m, dict) else m for m in data]
    return ids


@app.route("/api/llm/models", methods=["POST"])
def api_llm_models():
    data = request.get_json(force=True) or {}
    base = data.get("base_url") or config.LLM_BASE_URL
    api_key = data.get("api_key") or config.LLM_API_KEY
    try:
        ids = _llm_models(base, api_key)
        return jsonify({"ok": True, "models": ids, "base_url": _norm_url(base)})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 502


@app.route("/api/llm/chat", methods=["POST"])
def api_llm_chat():
    data = request.get_json(force=True) or {}
    base = data.get("base_url") or config.LLM_BASE_URL
    api_key = data.get("api_key") or config.LLM_API_KEY
    model = data.get("model") or config.LLM_MODEL
    messages = data.get("messages", [])
    if not model:
        return jsonify({"ok": False, "message": "No model selected."}), 400
    if not messages:
        return jsonify({"ok": False, "message": "No messages."}), 400
    try:
        url = _norm_url(base) + "/chat/completions"
        payload = {"model": model, "messages": messages, "stream": False}
        r = _requests.post(url, headers=_llm_headers(api_key), json=payload, timeout=120)
        if r.status_code >= 400:
            return jsonify({"ok": False, "message": f"HTTP {r.status_code}: {r.text[:400]}"}), \
                r.status_code
        data = r.json()
        content = ""
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except Exception:
            content = json.dumps(data)[:2000]
        # Keep any text until 'reasoning_content' (deepseek-style) is not blocked.
        return jsonify({"ok": True, "content": content, "model": model,
                        "usage": data.get("usage")})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 502


@app.route("/api/llm/config")
def api_llm_config():
    key = config.LLM_API_KEY
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("SET" if key else "")
    return jsonify({
        "api_key_set": bool(key),
        "api_key_masked": masked,
        "base_url": config.LLM_BASE_URL,
        "model": config.LLM_MODEL,
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
