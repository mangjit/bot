# OANDA Flip Bot

A small demo-account trading bot that keeps **one BUY and one SELL open** on a
currency pair, rides whichever side the market favours, and **flips the losing
side** to follow the move — running continuously until it hits a profit target
or a loss cap.

Tuned for a **$1 test run** (tiny trade sizes, stop out at the loss cap).

---

## 1. Get your OANDA demo account + API token (5 minutes)

1. Go to **https://www.oanda.com** → **Create an account** → choose **Practice /
   Demo** (free, no deposit).
2. When you create the account you can pick its **starting balance** — set it
   small (e.g. `$100`). The bot doesn't need a specific amount because it uses
   its own `STARTING_BALANCE`/`MAX_LOSS` budget; it just watches net P&L.
3. Once logged in, open **My Services → Manage API Access** (account security
   page).
4. Click **Generate** to create an **API token**. Copy it (shown once).
5. Find your **Account ID** — it's the number like `001-1234567-1234567890`,
   shown with the token or on the account dashboard.

> The bot talks to **`api-fxpractice.oanda.com`** (demo). It never touches the
> live `fxTrade` endpoint unless you change `OANDA_ENV=trade`.

---

## 2. Install and configure

```bash
cd oanda_flip_bot
pip install -r requirements.txt

cp .env.example .env
# open .env and paste your OANDA_API_TOKEN and OANDA_ACCOUNT_ID
```

(Or export them as environment variables — both work.)

---

## 3. Run it

```bash
python bot.py
```

You'll see a live log like:

```
09:00:01  INFO  Connected. Account=001-...-... starting net equity=$100.0000
09:00:01  INFO  Opened hedge: BUY 1000 + SELL 1000
09:00:06  INFO  [1] mid=1.08423 equity=$100.0012 P&L=+0.0012 (+0.12% of $1.00) | long_pl=+0.11 short_pl=-0.09 long_pips=+0.9 short_pips=-0.9
09:00:11  INFO    FLIP: short is losing (-11.2p). Closing losing short and re-opening it at market.
09:00:11  INFO  [2] mid=1.08501 equity=$100.0015 P&L=+0.0015 ...  ^FLIPPED
```

---

## 4. How the strategy works

- On start it opens a **long and a short** on the pair (hedge) — or a straddle
  of BUY/SELL STOPs if `ENTRY_MODE=straddle`.
- Every `POLL_INTERVAL` seconds it reads live prices. The side the market has
  moved **in favour of** is the winner; the other is the loser.
- When the **loser has lost `MIN_POSITION_SHIFT` pips**, the bot closes it and
  opens a **fresh order on that same side at the current market price** — this
  "re-centres" the grid and follows the trend while keeping a thin hedge.
- The bot stops and flattens everything when:
  - net profit reaches `PROFIT_TARGET`, **or**
  - net loss reaches `MAX_LOSS` (your safety cap), **or**
  - you press `Ctrl-C`.

## 5. The "$1 test" settings

| Setting | Default | Meaning |
|---|---|---|
| `STARTING_BALANCE` | `1.00` | Reference budget; P&L is shown as % of this. |
| `MAX_LOSS` | `1.00` | Stop if net loss ≥ $1 (100% of budget). |
| `PROFIT_TARGET` | `0.25` | Stop if net profit ≥ $0.25. |
| `OFFSET_PIPS` | `15` | Pip offset for the two legs / straddle. |
| `UNITS` | `1000` | Trade size per leg (0.01 lots). |
| `POLL_INTERVAL` | `5` | Seconds between checks. |
| `MIN_POSITION_SHIFT` | `10` | Pips a side must lose before it gets flipped. |

**Scaling up later:** increase `UNITS`, raise `MAX_LOSS`/`PROFIT_TARGET` to fit
your real account size, and (optionally, only when you know what you're doing)
point `OANDA_ENV=trade` at a live account.

---

## Important caveats

- **This is a demo/testing scaffold, not financial advice.** Past performance
  on a demo does not guarantee results live.
- Because you keep a hedge while flipping, **every flip pays the spread** and
  realises a small loss on the closed side. In a choppy market the strategy can
  bleed; the `MAX_LOSS` cap is what keeps a bad run from hurting you.
- On a demo account OANDA allows fractional units, so tiny sizes work. On live
  accounts minimum trade size applies (check the pair's contract specs).
- Never share your API token. It grants access to your account.

---

## Keep-alive / health check (free plan)

A free Render **web service spins down after ~15 min of no HTTP traffic**, which
would pause the trading bot. Two free ways to keep it alive:

### 1. GitHub Actions cron (recommended, free)

The repo includes `.github/workflows/keepalive.yml`. It pings your
`/health` endpoint every 3 minutes on GitHub's free runners.

**To enable:** go to your repo → **Settings → Secrets and variables → Actions →
New repository secret**, add:

| Name | Value |
|---|---|
| `RENDER_HEALTH_URL` | `https://<your-service>.onrender.com/health` |

Then the workflow runs automatically (and you can trigger it manually from the
**Actions** tab). If the ping returns non-200, the run is flagged red so you'll
notice the service is down.

### 2. UptimeRobot (free, no code)

Create a free monitor at uptimerobot.com pointing at
`https://<your-service>.onrender.com/health`, interval **5 minutes**. It does
the same thing externally.

### Local / Cloud Shell pinger

If you run the bot locally or in Cloud Shell, a simple loop works:

```bash
while true; do curl -sf http://localhost:5000/health >/dev/null && echo "ok $(date)"; sleep 300; done
```

---

## Note on the single-service (one-command) setup

`start.sh` runs `bot.py` + `telegram_bot.py` + the dashboard together. Because
they share the container, **if the dashboard spins down, the bot pauses too.**
So on the free plan the keep-alive above is important to keep the whole stack
running 24/7. For guaranteed always-on, use a paid Render instance or a VM.
