"""
OANDA Flip Bot  -  configuration
---------------------------------
Everything the bot needs, in one place. Edit these values (or better, set the
matching environment variables / .env file) before running.

The "$1 test" defaults are intentionally tiny so a bad run can't hurt.
"""

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

    # ---- The "$1" test config --------------------------------------------
    # The bot treats the account as having a $1 "risk budget". It records the
    # opening balance and stops if P&L falls to or below the loss cap.
    STARTING_BALANCE: float = float(_env("STARTING_BALANCE", "1.00"))
    # ---- Cumulative stop limits -------------------------------------------
    # Stop the bot once net P&L (realized + unrealized, measured from the
    # opening balance) reaches EITHER of these, whichever comes first.
    # MAX_LOSS   -> stop if cumulative LOSS >= this (stop-out safety)
    # PROFIT_TARGET -> stop if cumulative PROFIT >= this (take-profit)
    MAX_LOSS: float = float(_env("MAX_LOSS", "100.00"))
    PROFIT_TARGET: float = float(_env("PROFIT_TARGET", "100.00"))

    # ---- Order placement --------------------------------------------------
    # Distance (pips) above / below the market price for the two legs.
    OFFSET_PIPS: float = float(_env("OFFSET_PIPS", "15"))
    # Trade size per leg, in units. 10000 = 0.10 lots; for the $1 test go tiny.
    UNITS: int = int(_env("UNITS", "1000"))

    # Entry mode:
    #   "hedge"    -> open BOTH a BUY and a SELL at market (hedged), then flip.
    #   "straddle" -> place a BUY STOP and SELL STOP above/below price (breakout entry).
    ENTRY_MODE: str = _env("ENTRY_MODE", "hedge")

    # ---- Control loop -----------------------------------------------------
    POLL_INTERVAL: float = float(_env("POLL_INTERVAL", "5"))      # seconds between checks
    MIN_POSITION_SHIFT: float = float(_env("MIN_POSITION_SHIFT", "10"))  # pips a side must gain before we declare it "the winner"
    STOP_AFTER_ITERATIONS: int = int(_env("STOP_AFTER_ITERATIONS", "0"))  # 0 = run until target/loss/hand-stop

    # ---- Diagnostics ------------------------------------------------------
    VERBOSE: bool = _env("VERBOSE", "1") == "1"
    LOG_FILE: str = _env("LOG_FILE", "flip_bot.log")
    # JSON file the web dashboard reads for live results.
    STATE_FILE: str = _env("STATE_FILE", "state.json")

    @property
    def ticks_to_pips(self) -> float:
        # For most FX pairs 1 pip = 0.0001; JPY pairs = 0.01.
        return 0.01 if self.INSTRUMENT.endswith("_JPY") else 0.0001


config = Config()
