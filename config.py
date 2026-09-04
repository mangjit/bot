"""
OANDA Flip Bot  -  configuration
---------------------------------
Everything the bot needs, in one place. Edit these values (or better, set the
matching environment variables / .env file) before running.

The "$1 test" defaults are intentionally tiny so a bad run can't hurt.
"""

import json
import os
from dataclasses import dataclass, field

# Load values from a .env file (same folder) if present.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass  # python-dotenv optional; env vars still work


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    # ---- Connect to OANDA (REQUIRED) -------------------------------------
    # Fill these in, or set them as environment variables / in a .env file.
    API_TOKEN: str = _env("OANDA_API_TOKEN")        # e.g. "a1b2c3d4e5..."
    ACCOUNT_ID: str = _env("OANDA_ACCOUNT_ID")      # e.g. "001-1234567-1234567890"

    # Demo uses https://api-fxpractice.oanda.com  |  Live uses https://api-fxtrade.oanda.com
    ENV: str = _env("OANDA_ENV", "practice")        # "practice" (demo) or "trade" (live)
    @property
    def base_url(self) -> str:
        return "https://api-fxpractice.oanda.com" if self.ENV == "practice" \
            else "https://api-fxtrade.oanda.com"

    # ---- Instrument -------------------------------------------------------
    INSTRUMENT: str = _env("OANDA_INSTRUMENT", "EUR_USD")

    # ---- Risk budget (compounding) ----------------------------------------
    # The bot starts RISKING $1. As it banks profit, the amount it is willing
    # to risk grows ("budget = $1 + peak profit"), capped at MAX_RISK. If it
    # gives back the whole current budget (drawdown), it stops and closes.
    STARTING_BALANCE: float = float(_env("STARTING_BALANCE", "1.00"))  # initial risk budget
    MAX_LOSS: float = float(_env("MAX_LOSS", "100.00"))   # cap on the growing risk budget
    PROFIT_TARGET: float = float(_env("PROFIT_TARGET", "100.00"))  # stop after banking this much profit

    # ---- Order placement --------------------------------------------------
    # Distance (pips) above / below the market price for the two legs.
    OFFSET_PIPS: float = float(_env("OFFSET_PIPS", "15"))
    # Trade size per leg, in units. 10000 = 0.10 lots; for the $1 test go tiny.
    UNITS: int = int(_env("UNITS", "1000"))

    # Entry mode:
    #   "straddle" -> place a BUY STOP above and SELL STOP below price (breakout
    #                 entry). On a NETTING account (the default OANDA demo) this
    #                 is the mode that WORKS - it trades one direction at a time.
    #   "hedge"    -> open BOTH a BUY and a SELL at market (hedged). Requires a
    #                 HEDGING account; on a netting account they cancel to zero.
    ENTRY_MODE: str = _env("ENTRY_MODE", "straddle")

    # ---- Control loop -----------------------------------------------------
    POLL_INTERVAL: float = float(_env("POLL_INTERVAL", "5"))      # seconds between checks
    MIN_POSITION_SHIFT: float = float(_env("MIN_POSITION_SHIFT", "10"))  # pips a side must gain before we declare it "the winner"
    STOP_AFTER_ITERATIONS: int = int(_env("STOP_AFTER_ITERATIONS", "0"))  # 0 = run until target/loss/hand-stop

    # ---- Cloud LLM chat (OpenAI-compatible) --------------------------------
    # API key + base URL for any OpenAI-compatible endpoint (OpenAI, OpenRouter,
    # Groq, Together, a local Ollama/vLLM, etc.). Used by the dashboard "Chat"
    # tab to load models and chat. Set in the dashboard or via .env.
    LLM_API_KEY: str = _env("LLM_API_KEY")
    LLM_BASE_URL: str = _env("LLM_BASE_URL", "https://api.openai.com/v1")
    LLM_MODEL: str = _env("LLM_MODEL")

    # ---- Telegram ----------------------------------------------------------
    # Bot token from @BotFather. Optional - Telegram control is disabled if unset.
    TELEGRAM_TOKEN: str = _env("TELEGRAM_TOKEN")
    # Comma-separated list of Telegram user IDs allowed to control the bot.
    # Leave empty to allow anyone with the token to control it (not recommended).
    ALLOWED_USERS: str = _env("ALLOWED_USERS", "")

    # ---- Diagnostics ------------------------------------------------------
    VERBOSE: bool = _env("VERBOSE", "1") == "1"
    LOG_FILE: str = _env("LOG_FILE", "flip_bot.log")
    # JSON file the web dashboard reads for live results.
    STATE_FILE: str = _env("STATE_FILE", "state.json")
    # JSON file the Telegram bot writes live commands/settings to.
    CONTROL_FILE: str = _env("CONTROL_FILE", "control.json")

    @property
    def ticks_to_pips(self) -> float:
        # For most FX pairs 1 pip = 0.0001; JPY pairs = 0.01.
        return 0.01 if self.INSTRUMENT.endswith("_JPY") else 0.0001


config = Config()

# --------------------------------------------------------------------------- #
#  Runtime settings override (settings.json)
# --------------------------------------------------------------------------- #
# The dashboard "API Integration" panel writes credentials/settings here so you
# don't have to edit .env or restart the server. If the file exists, its values
# override the env-var defaults above (for BOTH bot.py and app.py).
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


def _load_settings_into_config():
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
    except Exception:
        return
    for key in ("API_TOKEN", "ACCOUNT_ID", "ENV", "INSTRUMENT", "ENTRY_MODE",
                "STARTING_BALANCE", "MAX_LOSS", "PROFIT_TARGET", "UNITS",
                "OFFSET_PIPS", "POLL_INTERVAL", "MIN_POSITION_SHIFT",
                "TELEGRAM_TOKEN", "ALLOWED_USERS",
                "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        if key in s and s[key] is not None:
            setattr(config, key, s[key])


def reset_runtime():
    """Re-load settings.json into the live config (after the dashboard saves)."""
    _load_settings_into_config()


_load_settings_into_config()
