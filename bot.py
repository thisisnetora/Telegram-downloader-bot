"""Telegram downloader bot — YouTube, Instagram, TikTok, Pinterest."""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

import yt_dlp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ChatAction, ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
FORCE_JOIN_CHANNEL = os.environ.get("FORCE_JOIN_CHANNEL", "").strip()  # e.g. @mychannel
MAX_FILE_SIZE = 49 * 1024 * 1024  # Telegram Bot API upload limit is 50 MB
COOKIES_FILE = os.environ.get("COOKIES_FILE", "cookies.txt")

# On hosts like Railway you can't upload files easily — pass cookies as an
# env var instead and we materialize the file at startup.
_cookies_content = os.environ.get("COOKIES_CONTENT", "").strip()
if _cookies_content and not Path(COOKIES_FILE).exists():
    Path(COOKIES_FILE).write_text(_cookies_content, encoding="utf-8")
BASE_WORK_DIR = Path(tempfile.gettempdir()) / "tg-downloader-bot"

URL_RE = re.compile(r"https?://[^\s<>'\"]+")

PLATFORMS = [
    ("youtube", re.compile(r"(youtube\.com|youtu\.be)", re.I)),
    ("instagram", re.compile(r"instagram\.com", re.I)),
    ("tiktok", re.compile(r"(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)", re.I)),
    ("pinterest", re.compile(r"(pinterest\.[a-z.]+|pin\.it)", re.I)),
]

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".opus", ".wav"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

pending: dict[str, str] = {}
download_sem = asyncio.Semaphore(3)

JOIN_REQUIRED = (
    "🔒 برای استفاده از ربات، اول باید عضو کانال ما بشی.\n\n"
    "بعد از عضویت، دکمه «✅ عضو شدم» رو بزن."
)


def join_keyboard() -> InlineKeyboardMarkup:
    link = os.environ.get("FORCE_JOIN_LINK", "").strip()
    if not link:
        link = f"https://t.me/{FORCE_JOIN_CHANNEL.lstrip('@')}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ عضویت در کانال", url=link)],
        [InlineKeyboardButton("✅ عضو شدم", callback_data="join:check")],
    ])


async def is_member(bot, user_id: int) -> bool:
    if not FORCE_JOIN_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(FORCE_JOIN_CHANNEL, user_id)
        if member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            return True
        return (
            member.status == ChatMemberStatus.RESTRICTED
            and getattr(member, "is_member", False)
        )
    except Exception as exc:
        logger.warning(
            "Membership check failed (%s). Is the bot an admin in %s?",
            exc, FORCE_JOIN_CHANNEL,
        )
        return True  # fail open so a misconfigured channel doesn't break the bot


async def require_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await is_member(context.bot, update.effective_user.id):
        return True
    if update.message:
        await update.message.reply_text(JOIN_REQUIRED, reply_markup=join_keyboard())
    elif update.callback_query:
        await update.callback_query.answer("❌ اول عضو کانال شو!", show_alert=True)
    return False

WELCOME = """
👋 سلام! به ربات دانلودر خوش اومدی

فقط کافیه لینک رو بفرستی، بقیه‌ش با من ⚡

📺 <b>یوتیوب</b> — ویدیو با کیفیت دلخواه یا MP3
📸 <b>اینستاگرام</b> — ریلز، پست و IGTV
🎵 <b>تیک‌تاک</b> — ویدیو و عکس
📌 <b>پینترست</b> — ویدیو و عکس

/help برای راهنما
"""

HELP = """
📖 <b>راهنمای استفاده</b>

۱. لینک پست رو کپی کن
۲. همین‌جا بفرست
۳. چند لحظه صبر کن تا فایل برسه ⏳

💡 <b>نکته‌ها:</b>
• برای یوتیوب می‌تونی کیفیت یا MP3 انتخاب کنی
• حداکثر حجم فایل: ۵۰ مگابایت (محدودیت تلگرام)
• اگر ویدیوی یوتیوب حجیم بود، کیفیت پایین‌تر رو امتحان کن
"""


def detect_platform(url: str):
    for name, pattern in PLATFORMS:
        if pattern.search(url):
            return name
    return None


def progress_bar(pct: float) -> str:
    filled = min(10, int(pct // 10))
    return "🟩" * filled + "⬜" * (10 - filled)


async def safe_edit(message, text: str):
    try:
        await message.edit_text(text)
    except Exception:
        pass


def make_progress_hook(loop, status_message, state):
    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                pct = downloaded / total * 100
                if pct - state.get("last", -10) >= 10:
                    state["last"] = pct
                    asyncio.run_coroutine_threadsafe(
                        safe_edit(
                            status_message,
                            f"⬇️ در حال دانلود...\n{progress_bar(pct)} {pct:.0f}%",
                        ),
                        loop,
                    )
        elif d.get("status") == "finished":
            asyncio.run_coroutine_threadsafe(
                safe_edit(status_message, "⚙️ در حال پردازش نهایی..."), loop
            )

    return hook


def download(url: str, workdir: Path, status_message, loop,
             quality: int | None = None, audio: bool = False) -> list[Path]:
    opts = {
        "outtmpl": str(workdir / "%(title).100s-%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "retries": 3,
        "writethumbnail": False,
        "writesubtitles": False,
        "progress_hooks": [make_progress_hook(loop, status_message, {})],
    }
    # A TikTok photo post or IG carousel IS a playlist — download all items.
    # For YouTube keep the single video only, even if the URL has &list=.
    if detect_platform(url) == "youtube":
        opts["noplaylist"] = True
    else:
        opts["noplaylist"] = False
        opts["playlistend"] = 10
        opts["ignoreerrors"] = True
    if audio:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif quality:
        opts["format"] = (
            f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={quality}]+bestaudio/"
            f"best[height<={quality}]/best"
        )
        opts["merge_output_format"] = "mp4"
    else:
        opts["format"] = (
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080]+bestaudio/"
            "best[ext=mp4]/best"
        )
        opts["merge_output_format"] = "mp4"

    if Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)

    return sorted(
        (p for p in workdir.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )


def fetch_og_image(url: str):
    import requests

    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0 Safari/537.36"},
        timeout=20,
    )
    for pattern in (
        r'property="og:image"\s+content="([^"]+)"',
        r'content="([^"]+)"\s+property="og:image"',
        r'"image_xlarge_url"\s*:\s*"([^"]+)"',
    ):
        m = re.search(pattern, resp.text)
        if m:
            return m.group(1).replace("&amp;", "&")
    return None


async def deliver(message, files: list[Path], caption: str = ""):
    images = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
    others = [f for f in files if f.suffix.lower() not in IMAGE_EXTS]

    if len(images) > 1:
        for i in range(0, len(images), 10):
            batch = images[i:i + 10]
            handles = [open(p, "rb") for p in batch]
            try:
                await message.reply_media_group([InputMediaPhoto(h) for h in handles])
            finally:
                for h in handles:
                    h.close()
    elif images:
        with open(images[0], "rb") as fh:
            await message.reply_photo(fh, caption=caption)

    for f in others:
        ext = f.suffix.lower()
        with open(f, "rb") as fh:
            if ext in VIDEO_EXTS:
                await message.reply_video(
                    fh, caption=caption, supports_streaming=True
                )
            elif ext in AUDIO_EXTS:
                await message.reply_audio(fh, caption=caption)
            else:
                await message.reply_document(fh, caption=caption)


async def process_download(message, url: str,
                           quality: int | None = None, audio: bool = False,
                           status_message=None):
    chat = message.chat
    status = status_message or await message.reply_text("⏳ در حال آماده‌سازی...")
    workdir = BASE_WORK_DIR / uuid.uuid4().hex[:12]
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        await chat.send_action(ChatAction.UPLOAD_VIDEO)
        loop = asyncio.get_running_loop()

        async with download_sem:
            await safe_edit(status, "⬇️ در حال دانلود...")
            files = await asyncio.to_thread(
                download, url, workdir, status, loop, quality, audio
            )

        # Some pins/posts are plain images — yt-dlp can't handle them.
        if not files:
            image_url = await asyncio.to_thread(fetch_og_image, url)
            if image_url:
                await status.delete()
                await message.reply_photo(image_url)
                return
            raise RuntimeError("nothing downloaded")

        ok = [f for f in files if f.stat().st_size <= MAX_FILE_SIZE]
        if not ok:
            size_mb = max(f.stat().st_size for f in files) / 1024 / 1024
            await safe_edit(
                status,
                f"⚠️ حجم فایل {size_mb:.0f} مگابایته و از محدودیت ۵۰ مگابایت "
                f"تلگرام بیشتره.\n\n"
                f"💡 برای یوتیوب: لینک رو دوباره بفرست و کیفیت پایین‌تر "
                f"یا MP3 رو انتخاب کن.",
            )
            return

        await safe_edit(status, "📤 در حال آپلود به تلگرام...")
        await chat.send_action(ChatAction.UPLOAD_VIDEO)
        await deliver(message, ok)
        await status.delete()

    except Exception as exc:
        logger.exception("Download failed: %s", url)
        reason = re.sub(r"^ERROR:\s*", "", str(exc)).replace(url, "").strip()
        if len(reason) > 250:
            reason = reason[:250] + "…"
        hint = ""
        if "sign in" in reason.lower() or "login" in reason.lower():
            hint = (
                "\n\n💡 یوتیوب/اینستاگرام IP سرور رو بلاک کرده. "
                "راه‌حل: فایل کوکی مرورگرت رو در متغیر COOKIES_CONTENT روی "
                "Railway ست کن (راهنما توی README)."
            )
        elif "instagram" in url:
            hint = "\n\n💡 بعضی پست‌های اینستاگرام نیاز به لاگین دارن — کوکی لازمه."
        await safe_edit(
            status,
            f"❌ دانلود نشد.\n\n🛠 دلیل:\n{reason or 'خطای ناشناخته'}{hint}",
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, context):
        return
    await update.message.reply_text(WELCOME, parse_mode="HTML")


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="HTML")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, context):
        return
    text = update.message.text or ""
    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text(
            "🔗 لینک یوتیوب، اینستاگرام، تیک‌تاک یا پینترست رو برام بفرست."
        )
        return

    url = match.group(0)
    platform = detect_platform(url)

    if platform == "youtube":
        token = uuid.uuid4().hex[:8]
        pending[token] = url
        keyboard = [
            [
                InlineKeyboardButton("📺 720p", callback_data=f"yt:720:{token}"),
                InlineKeyboardButton("📺 480p", callback_data=f"yt:480:{token}"),
                InlineKeyboardButton("📺 360p", callback_data=f"yt:360:{token}"),
            ],
            [InlineKeyboardButton("🎵 MP3 (فقط صوت)", callback_data=f"yt:mp3:{token}")],
        ]
        await update.message.reply_text(
            "🎬 لینک یوتیوب دریافت شد!\nکیفیت رو انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await process_download(update.message, url)


async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_membership(update, context):
        return
    try:
        _, q, token = query.data.split(":")
    except ValueError:
        return

    url = pending.pop(token, None)
    if not url:
        await safe_edit(query.message, "⌛ این درخواست منقضی شده. لینک رو دوباره بفرست.")
        return

    audio = q == "mp3"
    quality = None if audio else int(q)
    await process_download(
        query.message, url, quality=quality, audio=audio, status_message=query.message
    )


async def on_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await is_member(context.bot, query.from_user.id):
        await query.answer("✅ عضویت تأیید شد!")
        await safe_edit(query.message, "✅ عضویتت تأیید شد! حالا لینک رو بفرست 🚀")
    else:
        await query.answer("❌ هنوز عضو کانال نشدی!", show_alert=True)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ متغیر محیطی BOT_TOKEN تنظیم نشده!")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^yt:"))
    app.add_handler(CallbackQueryHandler(on_join_check, pattern=r"^join:check$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    logger.info("Bot is starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
