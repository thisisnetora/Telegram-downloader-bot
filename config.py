import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_NAME = os.getenv("BOT_NAME", "Netora AI")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# چنلی که کاربر باید عضو آن باشد؛ مثل @mychannel یا آیدی عددی -100...
CHANNEL_ID = os.environ["CHANNEL_ID"]
CHANNEL_LINK = os.getenv("CHANNEL_LINK") or (
    f"https://t.me/{CHANNEL_ID.lstrip('@')}" if CHANNEL_ID.startswith("@") else ""
)

# آیدی عددی ادمین‌ها، جدا شده با کاما
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

DEFAULT_REQUIRED_REFERRALS = int(os.getenv("REQUIRED_REFERRALS", "3"))
DEFAULT_DAILY_CHAT_LIMIT = int(os.getenv("DAILY_CHAT_LIMIT", "20"))
DEFAULT_DAILY_IMAGE_LIMIT = int(os.getenv("DAILY_IMAGE_LIMIT", "3"))

DB_PATH = os.getenv("DB_PATH", "bot.db")

# بکاپ خودکار روزانه برای ادمین‌ها (ساعت بر اساس تایم سرور)
AUTO_BACKUP = os.getenv("AUTO_BACKUP", "true").lower() in ("1", "true", "yes")
BACKUP_HOUR = int(os.getenv("BACKUP_HOUR", "3"))
