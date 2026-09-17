import os
import logging
import yfinance as yf
from curl_cffi import requests as curl_requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ----------------------------------------------------------------------
# تنظیمات (از Environment Variables خوانده می‌شوند - در Railway ست کنید)
# ----------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # آیدی چتی که باید هشدار بهش ارسال بشه
SYMBOL = os.environ.get("STOCK_SYMBOL", "NVDA")

# آستانه افت (درصد) - وقتی افت بین این دو مقدار باشه هشدار می‌فرسته
DROP_MIN = float(os.environ.get("DROP_MIN", "3"))   # حداقل 3 درصد
DROP_MAX = float(os.environ.get("DROP_MAX", "100")) # عملا بدون سقف بالا (هر افت >=3% هم پوشش داده میشه)

CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))  # هر 5 دقیقه

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# برای جلوگیری از اسپم هشدار در یک روز
already_alerted_today = {"date": None, "alerted": False}

# یاهو فایننس روی سرورهای ابری (از جمله Railway) بعضی وقت‌ها درخواست‌های ساده رو
# بلاک می‌کنه. با یه سشن curl_cffi که شبیه یه مرورگر واقعی (کروم) رفتار می‌کنه،
# این مشکل معمولا حل میشه.
_session = curl_requests.Session(impersonate="chrome")


def get_stock_change(symbol: str):
    """
    قیمت لحظه‌ای و درصد تغییر سهم رو نسبت به close روز قبل برمی‌گردونه.
    خروجی: (current_price, prev_close, percent_change) یا None در صورت خطا
    """
    # روش اول: fast_info
    try:
        ticker = yf.Ticker(symbol, session=_session)
        fast_info = ticker.fast_info

        current_price = fast_info["last_price"]
        prev_close = fast_info["previous_close"]

        if current_price and prev_close:
            percent_change = ((current_price - prev_close) / prev_close) * 100
            return current_price, prev_close, percent_change
    except Exception as e:
        logger.warning(f"روش fast_info شکست خورد، تلاش با history: {e}")

    # روش دوم (پشتیبان): گرفتن قیمت از تاریخچه چند روز اخیر
    try:
        ticker = yf.Ticker(symbol, session=_session)
        hist = ticker.history(period="5d", interval="1d")

        if hist is None or len(hist) < 2:
            logger.error("داده تاریخچه کافی برای محاسبه تغییر قیمت وجود نداره.")
            return None

        current_price = float(hist["Close"].iloc[-1])
        prev_close = float(hist["Close"].iloc[-2])
        percent_change = ((current_price - prev_close) / prev_close) * 100
        return current_price, prev_close, percent_change
    except Exception as e:
        logger.error("خطا در دریافت داده سهم (هر دو روش شکست خوردند):", exc_info=True)
        return None


def format_message(symbol, current_price, prev_close, percent_change):
    arrow = "🔻" if percent_change < 0 else "🔺"
    return (
        f"{arrow} سهام {symbol}\n\n"
        f"قیمت فعلی: {current_price:.2f}$\n"
        f"قیمت بسته‌شدن قبلی: {prev_close:.2f}$\n"
        f"تغییر: {percent_change:.2f}%"
    )


# ----------------------------------------------------------------------
# دستورات ربات
# ----------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! ربات رصد سهام روشنه.\n\n"
        f"سهم تحت نظر: {SYMBOL}\n"
        f"دستور /check رو بفرست تا وضعیت لحظه‌ای رو ببینی.\n\n"
        f"چت آیدی شما: {update.effective_chat.id}\n"
        "(اگه می‌خوای هشدارها به همین چت ارسال بشه، این عدد رو در متغیر "
        "TELEGRAM_CHAT_ID در Railway ست کن)"
    )


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = get_stock_change(SYMBOL)
    if result is None:
        await update.message.reply_text("⚠️ در حال حاضر نمی‌تونم قیمت رو دریافت کنم. دوباره تلاش کن.")
        return

    current_price, prev_close, percent_change = result
    await update.message.reply_text(format_message(SYMBOL, current_price, prev_close, percent_change))


# ----------------------------------------------------------------------
# چک دوره‌ای پس‌زمینه برای هشدار افت قیمت
# ----------------------------------------------------------------------
async def periodic_check(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID:
        return  # اگه چت آیدی ست نشده، هشداری ارسال نمیشه

    result = get_stock_change(SYMBOL)
    if result is None:
        return

    current_price, prev_close, percent_change = result

    import datetime
    today = datetime.date.today().isoformat()

    # ریست کردن وضعیت هشدار در ابتدای هر روز جدید
    if already_alerted_today["date"] != today:
        already_alerted_today["date"] = today
        already_alerted_today["alerted"] = False

    drop = -percent_change  # اگه سهم افت کرده باشه، این عدد مثبت میشه

    if DROP_MIN <= drop <= DROP_MAX and not already_alerted_today["alerted"]:
        text = (
            "🚨 هشدار افت قیمت سهام!\n\n"
            + format_message(SYMBOL, current_price, prev_close, percent_change)
        )
        await context.bot.send_message(chat_id=CHAT_ID, text=text)
        already_alerted_today["alerted"] = True
        logger.info(f"هشدار ارسال شد. افت: {drop:.2f}%")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("متغیر TELEGRAM_BOT_TOKEN ست نشده است.")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("check", check))

    # چک دوره‌ای هر CHECK_INTERVAL_SECONDS ثانیه
    application.job_queue.run_repeating(
        periodic_check, interval=CHECK_INTERVAL_SECONDS, first=10
    )

    logger.info("ربات در حال اجراست...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
