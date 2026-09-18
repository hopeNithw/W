# -*- coding: utf-8 -*-
"""
NVDA Telegram Alert Bot — Railway deployment version
------------------------------------------------------
- /nvda -> replies with the current NVDA price and % change (text only)
- Automatically sends a big warning alert if NVDA drops X% or more
  from the previous close (default 3%, configurable via env var).

Configuration is done via environment variables (set these in the
Railway dashboard -> your service -> Variables tab):

    BOT_TOKEN             (required) your Telegram bot token
    ALERT_DROP_PERCENT    (optional) default 3.0
    CHECK_INTERVAL        (optional) seconds between price checks, default 60
    CACHE_TTL             (optional) seconds to cache the last price, default 15
                           (protects against Yahoo Finance rate limiting / 429s)

No hardcoded secrets, no Android-only code — safe to run in any
standard Linux container.
"""

import os
import sys
import time
import logging
import requests

# ----------------------------------------------------------------------
# CONFIG (from environment variables)
# ----------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
SYMBOL = os.environ.get("SYMBOL", "NVDA").strip()
ALERT_DROP_PERCENT = float(os.environ.get("ALERT_DROP_PERCENT", "3.0"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
POLL_TIMEOUT = 20  # long-poll timeout for getUpdates, in seconds

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("nvda-bot")

# runtime state for the alert feature (kept in memory)
_alert_chat_ids = set()
_alerted = False

# a single requests session, reused for connection pooling / retries
session = requests.Session()
session.headers.update({"User-Agent": "nvda-telegram-bot/1.0"})


# ----------------------------------------------------------------------
# STOCK DATA
# ----------------------------------------------------------------------
_YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]

# simple in-memory cache so rapid repeated /nvda commands (or the alert
# checker) don't hammer Yahoo and trigger rate limiting (HTTP 429)
CACHE_TTL = int(os.environ.get("CACHE_TTL", "15"))  # seconds
_cache = {"data": None, "ts": 0.0}


def _fetch_stock_data_raw(symbol):
    """One real network call to Yahoo Finance, with retry/backoff on
    429 (rate limited) and 5xx errors, and host rotation."""
    params = {"interval": "1m", "range": "1d", "includePrePost": "true"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    last_error = None
    delay = 1.5
    attempts = 4

    for attempt in range(attempts):
        host = _YAHOO_HOSTS[attempt % len(_YAHOO_HOSTS)]
        url = f"https://{host}/v8/finance/chart/{symbol}"
        try:
            resp = session.get(url, params=params, headers=headers, timeout=10)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = requests.exceptions.HTTPError(
                    f"{resp.status_code} from {host}"
                )
                log.warning(
                    "Yahoo returned %s (attempt %d/%d), backing off %.1fs",
                    resp.status_code, attempt + 1, attempts, delay,
                )
                time.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_error = e
            log.warning("Fetch attempt %d/%d failed: %s", attempt + 1, attempts, e)
            time.sleep(delay)
            delay *= 2

    raise last_error or RuntimeError("Failed to fetch stock data")


def fetch_stock_data(symbol=None, use_cache=True):
    """Fetch the LIVE price (pre-market / after-hours / regular session,
    whichever is currently active) and % change vs previous close.
    Cached for CACHE_TTL seconds to avoid rate limiting. No API key needed."""
    symbol = symbol or SYMBOL

    now = time.time()
    if use_cache and _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    try:
        payload = _fetch_stock_data_raw(symbol)
    except Exception:
        # if we have a slightly stale cached value, prefer that over an error
        if _cache["data"] is not None:
            log.warning("Live fetch failed, serving last cached price instead")
            return _cache["data"]
        raise

    result_list = payload.get("chart", {}).get("result")
    if not result_list:
        raise ValueError(f"No chart data returned for {symbol}")

    meta = result_list[0]["meta"]
    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
    exchange = meta.get("exchangeName", "NASDAQ")
    market_state = meta.get("marketState", "REGULAR")  # PRE, REGULAR, POST, CLOSED

    # pick whichever price is actually live right now
    if market_state == "PRE" and meta.get("preMarketPrice"):
        price = meta["preMarketPrice"]
        session_label = "Pre-market"
    elif market_state == "POST" and meta.get("postMarketPrice"):
        price = meta["postMarketPrice"]
        session_label = "After-hours"
    elif market_state == "REGULAR" and meta.get("regularMarketPrice"):
        price = meta["regularMarketPrice"]
        session_label = "Live"
    else:
        # market closed and no pre/post tick available -> last known price
        price = meta.get("regularMarketPrice")
        session_label = "Closed"

    if price is None or prev_close is None:
        raise ValueError(f"Incomplete data returned for {symbol}")

    pct_change = ((price - prev_close) / prev_close) * 100

    data = {
        "symbol": symbol,
        "exchange": exchange,
        "price": price,
        "pct_change": pct_change,
        "session": session_label,
        "updated": time.strftime("%H:%M UTC", time.gmtime()),
    }
    _cache["data"] = data
    _cache["ts"] = now
    return data


# ----------------------------------------------------------------------
# TELEGRAM HELPERS
# ----------------------------------------------------------------------
def send_text(chat_id, text):
    try:
        r = session.post(
            f"{API_BASE}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.warning("Failed to send message to %s: %s", chat_id, e)


def get_updates(offset=None):
    params = {"timeout": POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    r = session.get(f"{API_BASE}/getUpdates", params=params, timeout=POLL_TIMEOUT + 10)
    r.raise_for_status()
    return r.json().get("result", [])


# ----------------------------------------------------------------------
# COMMAND HANDLING
# ----------------------------------------------------------------------
def handle_message(chat_id, text):
    _alert_chat_ids.add(chat_id)
    text = (text or "").strip().lower()

    if text in ("/start", "/help"):
        send_text(
            chat_id,
            f"Send /{SYMBOL.lower()} to get the latest price.\n"
            f"You'll also get an automatic \U0001F6A8 alert if {SYMBOL} "
            f"drops {ALERT_DROP_PERCENT:.1f}%+ from the previous close.",
        )
        return

    if text.startswith(f"/{SYMBOL.lower()}") or text.startswith("/nvda"):
        try:
            data = fetch_stock_data()
            sign = "+" if data["pct_change"] >= 0 else ""
            msg = (
                f"{data['symbol']} \u2022 {data['exchange']} \u2022 {data['session']}\n"
                f"${data['price']:.2f}  ({sign}{data['pct_change']:.2f}%)\n"
                f"Updated {data['updated']}"
            )
            send_text(chat_id, msg)
        except Exception as e:
            log.exception("Failed to fetch stock data")
            send_text(chat_id, f"Error fetching {SYMBOL} data: {e}")


def check_crash_alert():
    """Check the price drop and blast an alert to all known chats if triggered."""
    global _alerted

    if not _alert_chat_ids:
        return  # nobody has talked to the bot yet

    try:
        data = fetch_stock_data()
    except Exception as e:
        log.warning("Alert check failed: %s", e)
        return

    drop = -data["pct_change"]  # positive number = how much it has dropped

    if drop >= ALERT_DROP_PERCENT and not _alerted:
        alert_msg = (
            "\U0001F6A8\U0001F6A8\U0001F6A8 \u0647\u0634\u062f\u0627\u0631! "
            "\u0633\u0642\u0648\u0637 \u0634\u062f\u06cc\u062f \u0633\u0647\u0627\u0645 "
            "\U0001F6A8\U0001F6A8\U0001F6A8\n"
            f"\U0001F4C9\U0001F4C9\U0001F4C9 {data['symbol']} \u0627\u0641\u062a \u06a9\u0631\u062f! "
            "\U0001F4C9\U0001F4C9\U0001F4C9\n\n"
            f"\u26A0\uFE0F \u0642\u06CC\u0645\u062A: ${data['price']:.2f}\n"
            f"\U0001F53B \u062A\u063A\u06CC\u06CC\u0631: {data['pct_change']:.2f}%\n"
            f"\U0001F551 \u0633\u0627\u0639\u062A: {data['updated']}\n\n"
            "\U0001F6D1 \u0645\u0631\u0627\u0642\u0628 \u0628\u0627\u0632\u0627\u0631 \u0628\u0627\u0634!"
        )
        for cid in list(_alert_chat_ids):
            send_text(cid, alert_msg)
        _alerted = True
        log.info("Crash alert sent (drop=%.2f%%)", drop)

    elif drop < ALERT_DROP_PERCENT - 1:
        # price recovered a bit -> re-arm so a future drop can trigger again
        if _alerted:
            log.info("Price recovered, re-arming alert")
        _alerted = False


# ----------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN environment variable is not set. Exiting.")
        sys.exit(1)

    log.info(
        "Bot starting. symbol=%s alert_drop=%.1f%% check_interval=%ss",
        SYMBOL, ALERT_DROP_PERCENT, CHECK_INTERVAL,
    )

    offset = None
    last_check = 0.0

    while True:
        try:
            updates = get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                handle_message(chat_id, msg.get("text", ""))

            if time.time() - last_check >= CHECK_INTERVAL:
                check_crash_alert()
                last_check = time.time()

        except requests.exceptions.RequestException as e:
            log.warning("Network error, retrying in 5s: %s", e)
            time.sleep(5)
        except Exception:
            # never let an unexpected error kill the whole process on Railway
            log.exception("Unexpected error in main loop, continuing")
            time.sleep(5)


if __name__ == "__main__":
    main()
