"""
OANDA Flip Bot  -  Telegram control daemon
------------------------------------------
Runs a Telegram bot (via the raw Bot API, long-polling with `requests`) that
manages the trading bot (bot.py) as a subprocess and lets you:

    /start                 launch the trading bot (open/flip positions)
    /stop                  gracefully close everything and stop
    /status                live account + bot status
    /profit <amount>       set profit target ($)
    /loss   <amount>       set max loss cap ($)
    /risk   <amount>       set risk budget base ($ - the "$1 start" grows from here)
    /units  <amount>       set trade size per position in units (1000 = 0.01 lots)
    /recommend             get a recommendation based on current performance
    /help                  show this

It writes live settings to CONTROL_FILE, which bot.py reads each tick, so
settings change without restarting the bot. It records the chat_id so the
trading bot can send it notifications (flips, target hit, loss cap).

Usage:
    set OANDA_API_TOKEN, OANDA_ACCOUNT_ID, TELEGRAM_TOKEN (and optionally
    ALLOWED_USERS) then:   python telegram_bot.py

NOTE: on Render/workers this runs alongside bot.py; on Cloud Shell it can
launch bot.py as a subprocess.
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from config import config

API = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}" if config.TELEGRAM_TOKEN else None
POLL_TIMEOUT = 40
BOT_PROC = None
LAST_CHAT = None
_control_dir = os.path.dirname(os.path.abspath(__file__))
ALLOWED = set()
if config.ALLOWED_USERS:
    ALLOWED = {int(x) for x in config.ALLOWED_USERS.split(",") if x.strip()}


# ---- helpers ------------------------------------------------------------ #
def api(method, **params):
    if not API:
        return None
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=POLL_TIMEOUT + 5)
        return r.json()
    except Exception as e:
        print(f"[telegram] api error: {e}")
        return None


def send(chat_id, text):
    api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


def authorized(user_id):
    return (not ALLOWED) or user_id in ALLOWED


def write_control(**kv):
    path = os.path.join(_control_dir, config.CONTROL_FILE)
    data = {}
    if os.path.exists(path):
        try:
            data = json.loads(Path(path).read_text())
        except Exception:
            data = {}
    data.update(kv)
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    Path(path).write_text(json.dumps(data, indent=2))
    return data


def read_state():
    path = os.path.join(_control_dir, config.STATE_FILE)
    if os.path.exists(path):
        try:
            return json.loads(Path(path).read_text())
        except Exception:
            return None
    return None


def clear_control_flag(key, default):
    """Set a one-shot flag back to default after it's been consumed."""
    path = os.path.join(_control_dir, config.CONTROL_FILE)
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return
    if data.get(key) != default:
        data[key] = default
        Path(path).write_text(json.dumps(data, indent=2))


def bot_running():
    return BOT_PROC is not None and BOT_PROC.poll() is None


# ---- commands ----------------------------------------------------------- #
def cmd_start(chat_id):
    global BOT_PROC
    if not (config.API_TOKEN and config.ACCOUNT_ID):
        send(chat_id, "⚠️ OANDA_API_TOKEN / OANDA_ACCOUNT_ID not set. Cannot start.")
        return
    if bot_running():
        send(chat_id, "🟢 Bot is already running.")
        return
    # clear any prior /stop flag so bot.py doesn't immediately exit
    write_control(stop=False, chat_id=chat_id)
    env = os.environ.copy()
    # ensure settings from control.json reach the subprocess via config defaults
    BOT_PROC = subprocess.Popen(
        [sys.executable, "bot.py"], cwd=_control_dir, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    write_control(stop=False, chat_id=chat_id)
    send(chat_id, f"✅ Bot STARTED (pid {BOT_PROC.pid}) on {config.INSTRUMENT} "
                  f"in mode={config.ENTRY_MODE}.")


def cmd_stop(chat_id):
    global BOT_PROC
    # Ask the trading loop to close positions & exit (graceful).
    write_control(stop=True, chat_id=chat_id)
    if bot_running():
        BOT_PROC.send_signal(signal.SIGINT)  # bot.py handles KeyboardInterrupt
        try:
            BOT_PROC.wait(timeout=15)
        except Exception:
            BOT_PROC.terminate()
    send(chat_id, "🛑 Stop requested. Closing open positions and halting.")


def cmd_status(chat_id):
    st = read_state() or {}
    if not st:
        send(chat_id, f"<b>Status</b>\nNo state yet. Bot likely not running.\n"
                      f"Run /start to begin.")
        return
    running = "🟢 RUNNING" if st.get("running") else "⏸ STOPPED"
    reason = st.get("reason") or ""
    instr = st.get("instrument", config.INSTRUMENT)
    pnl = st.get("pnl", 0)
    pct = st.get("pnl_pct", 0)
    eq = st.get("equity", 0)
    budget = st.get("risk_budget", 0)
    peak = st.get("peak_pnl", 0)
    target = st.get("target", config.PROFIT_TARGET)
    maxloss = st.get("max_loss", config.MAX_LOSS)
    legs = st.get("legs")
    flips = st.get("flips") or []
    leg_txt = "no open position"
    if legs:
        leg_txt = (f"BUY pl={legs['long']['pl']:+.2f} @ {legs['long'].get('entry',0):.5f}\n"
                   f"SELL pl={legs['short']['pl']:+.2f} @ {legs['short'].get('entry',0):.5f}")
    txt = (f"<b>{running}</b> {config.INSTRUMENT}"
           + (f" ({reason})" if reason else "") + "\n"
           f"Mode: {st.get('mode', config.ENTRY_MODE)}, units={config.UNITS}\n"
           f"Equity: ${eq:,.2f}\n"
           f"P&amp;L: <b>{pnl:+.2f}</b> ({pct:+.1f}%)\n"
           f"Risk budget: ${budget:,.2f} | Peak profit: ${peak:,.2f}\n"
           f"Target: ${target:,.2f} | Max loss: ${maxloss:,.2f}\n"
           f"Flips: {len(flips)}\n"
           f"<b>Position:</b>\n{leg_txt}")
    send(chat_id, txt)


def cmd_recommend(chat_id):
    st = read_state() or {}
    pnl = st.get("pnl", 0)
    peak = st.get("peak_pnl", 0)
    flips = st.get("flips") or []
    unit = config.UNITS
    risk = st.get("risk_budget", config.STARTING_BALANCE)
    target = st.get("target", config.PROFIT_TARGET)
    # Determine win/loss tendency from stored stats if present.
    win = st.get("wins", 0)
    loss = st.get("losses", 0)
    wl = (win / (win + loss) * 100) if (win + loss) else None

    lines = ["<b>ℹ️ Recommendation</b>"]
    lines.append(f"Current P&L: ${pnl:+.2f} | Peak: ${peak:+.2f} | Risk budget: ${risk:,.2f}")

    if pnl <= -0.8 * risk:
        lines.append("\n🔴 <b>Risk is high relative to budget.</b> Suggests lowering UNITS "
                     f"(currently {unit}) or /loss to a smaller cap. Consider /stop and re-evaluating.")
    elif pnl >= 0.5 * target:
        lines.append("\n🟢 <b>Doing well.</b> Over halfway to target. Consider taking profit "
                     "proactively with /stop, or /units up if you're confident.")
    elif not flips and pnl == 0:
        lines.append("\n🟡 <b>No trades yet.</b> Waiting for a breakout on "
                     f"{config.INSTRUMENT}. If the market is chopping, lower OFFSET_PIPS for "
                     "faster entries.")
    else:
        lines.append("\n⚪ <b>Unchanged.</b> Let it run. Keep the loss cap as-is.")

    if wl is not None:
        lines.append(f"\nWin rate: {wl:.0f}% ({win}W/{loss}L)")
    else:
        lines.append("\n(Flip-level win/loss stats accumulate as the bot runs.)")
    lines.append("\n<b>Tip:</b> Start small (UNITS=1000). Only raise lot size after the strategy "
                 "shows consistent profit over many flips.")
    send(chat_id, "\n".join(lines))


# ---- command router ----------------------------------------------------- #
def handle(chat_id, text):
    global LAST_CHAT
    LAST_CHAT = chat_id
    parts = text.strip().split()
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else None

    if cmd in ("/start", "/go"):
        cmd_start(chat_id)
    elif cmd in ("/stop", "/halt"):
        cmd_stop(chat_id)
    elif cmd in ("/status", "/state", "/info"):
        cmd_status(chat_id)
    elif cmd in ("/recommend", "/suggest", "/advice"):
        cmd_recommend(chat_id)
    elif cmd in ("/profit", "/target"):
        _set_float(chat_id, arg, "profit", "Profit target")
    elif cmd in ("/loss", "/maxloss"):
        _set_float(chat_id, arg, "loss", "Max loss cap")
    elif cmd in ("/risk", "/bankroll"):
        _set_float(chat_id, arg, "risk", "Risk budget")
    elif cmd in ("/units", "/lots"):
        _set_int(chat_id, arg, "units", "Units per position")
    elif cmd in ("/help", "/menu"):
        send(chat_id, HELP)
    else:
        send(chat_id, f"Unknown command {cmd}. See /help.")


def _set_float(chat_id, arg, key, label):
    if arg is None:
        send(chat_id, f"Usage: /{key} <amount>  e.g. /{key} 50")
        return
    try:
        val = float(arg)
        write_control(**{key: val, "chat_id": chat_id})
        send(chat_id, f"✅ {label} set to <b>${val:,.2f}</b> (applied live).")
    except ValueError:
        send(chat_id, f"❌ {arg!r} isn't a number. e.g. /{key} 50")


def _set_int(chat_id, arg, key, label):
    if arg is None:
        send(chat_id, f"Usage: /{key} <amount>  e.g. /{key} 5000")
        return
    try:
        val = int(arg)
        write_control(**{key: val, "chat_id": chat_id})
        send(chat_id, f"✅ {label} set to <b>{val:,}</b> (applied live).")
    except ValueError:
        send(chat_id, f"❌ {arg!r} isn't a whole number. e.g. /{key} 5000")


# ---- polling loop ------------------------------------------------------- #
def main():
    global BOT_PROC, LAST_CHAT
    if not config.TELEGRAM_TOKEN:
        print("Set TELEGRAM_TOKEN in the environment/.env to use Telegram control.")
        sys.exit(1)

    me = api("getMe")
    if not me or not me.get("ok"):
        print(f"Invalid TELEGRAM_TOKEN or network error: {me}")
        sys.exit(1)
    print(f"[telegram] Connected as @{me['result']['username']}")
    print("[telegram] Awaiting commands (Ctrl-C to quit).")

    offset = 0
    while True:
        upd = api("getUpdates", offset=offset, timeout=POLL_TIMEOUT, allowed_updates=["message"])
        if not upd or not upd.get("ok"):
            time.sleep(5)
            continue
        for u in upd["result"]:
            offset = max(offset, u["update_id"] + 1)
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            user = msg.get("from") or {}
            uid = user.get("id")
            text = (msg.get("text") or "").strip()
            if not text or text.startswith("/"):
                if text and not authorized(uid):
                    send(chat_id, "⛔ Not authorized to control this bot.")
                    continue
                if text:
                    handle(chat_id, text)
        time.sleep(0.5)


HELP = (
    "<b>OANDA Flip Bot — Telegram control</b>\n\n"
    "/start — launch the bot (open/flip trades)\n"
    "/stop — close everything and halt\n"
    "/status — live account &amp; bot status\n"
    "/profit &lt;amt&gt; — set profit target ($)\n"
    "/loss &lt;amt&gt; — set max loss cap ($)\n"
    "/risk &lt;amt&gt; — set risk budget base ($)\n"
    "/units &lt;amt&gt; — set trade size (units)\n"
    "/recommend — get a strategy recommendation\n"
    "/help — this message\n\n"
    "Settings apply live without restarting the bot."
)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[telegram] stopped.")
