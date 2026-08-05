"""Telegram downloader bot — YouTube, Instagram, TikTok, Pinterest."""

import asyncio
import atexit
import base64
import html
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
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
    ApplicationHandlerStop,
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
BOT_BRAND = os.environ.get("BOT_BRAND", "Netora").strip() or "Netora"
FORCE_JOIN_CHANNEL = os.environ.get("FORCE_JOIN_CHANNEL", "").strip()  # e.g. @mychannel
# Telegram wants @username or a numeric -100... id — normalize a bare username.
if (FORCE_JOIN_CHANNEL
        and not FORCE_JOIN_CHANNEL.startswith("@")
        and not FORCE_JOIN_CHANNEL.lstrip("-").isdigit()):
    FORCE_JOIN_CHANNEL = "@" + FORCE_JOIN_CHANNEL
# Official Bot API is capped at 50 MB. Point BOT_API_URL at a self-hosted
# telegram-bot-api server (runs in --local mode) to raise it to ~2000 MB.
BOT_API_URL = os.environ.get("BOT_API_URL", "").strip().rstrip("/")
if BOT_API_URL and not BOT_API_URL.startswith(("http://", "https://")):
    BOT_API_URL = f"http://{BOT_API_URL}"
try:
    MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_MB", "").strip() or "49") * 1024 * 1024
except ValueError:
    MAX_FILE_SIZE = 49 * 1024 * 1024
COOKIES_FILE = os.environ.get("COOKIES_FILE", "cookies.txt")

# On hosts like Railway you can't upload files easily — pass cookies as an
# env var instead and we materialize the file at startup. Base64 is the
# safest option: multiline env vars can get mangled by some dashboards.
_cookies_b64 = os.environ.get("COOKIES_B64", "").strip()
_cookies_content = os.environ.get("COOKIES_CONTENT", "").strip()
# The env var is the source of truth — always (re)write so a stale cookies.txt
# (e.g. one left on a persistent volume) never shadows fresh cookies.
if _cookies_b64:
    try:
        Path(COOKIES_FILE).write_bytes(base64.b64decode(_cookies_b64, validate=True))
    except Exception:
        logger.exception("COOKIES_B64 is not valid base64")
elif _cookies_content:
    # Some dashboards mangle multiline env vars into literal "\n" sequences.
    Path(COOKIES_FILE).write_text(
        _cookies_content.replace("\\n", "\n"), encoding="utf-8"
    )

if Path(COOKIES_FILE).exists():
    _lines = Path(COOKIES_FILE).read_text(encoding="utf-8", errors="replace").splitlines()
    logger.info("Cookies file loaded: %d lines", len(_lines))
else:
    logger.warning("No cookies file — YouTube will likely block datacenter IPs")
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

# --- Admin panel & persistent stats -----------------------------------------
ADMIN_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", "").strip())
    if x.isdigit()
}
DATA_FILE = os.environ.get("DATA_FILE", "bot_data.json")
# Day boundaries for "today" stats follow Iran time.
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))
START_TIME = time.time()

_DB_DEFAULT = {
    "users": {},          # str(user_id) -> {name, username, first, last}
    "total_downloads": 0,
    "total_failed": 0,
    "total_joins": 0,     # users who verified channel membership
    "platforms": {},      # platform -> count
    "daily": {},          # "YYYY-MM-DD" -> {downloads, failed, joins, new_users}
}


def _load_db() -> dict:
    try:
        data = json.loads(Path(DATA_FILE).read_text(encoding="utf-8"))
        return {**_DB_DEFAULT, **data}
    except Exception:
        return dict(_DB_DEFAULT)


db = _load_db()


def save_db():
    try:
        tmp = Path(DATA_FILE).with_suffix(".tmp")
        tmp.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
        tmp.replace(DATA_FILE)
    except Exception:
        logger.exception("Could not save stats db")


def today_str() -> str:
    return datetime.now(IRAN_TZ).date().isoformat()


def _daily() -> dict:
    return db["daily"].setdefault(today_str(), {
        "downloads": 0, "failed": 0, "joins": 0, "new_users": 0,
    })


def track_user(user) -> None:
    if not user:
        return
    uid = str(user.id)
    now = datetime.now(IRAN_TZ).isoformat(timespec="seconds")
    if uid not in db["users"]:
        db["users"][uid] = {
            "name": user.full_name or "",
            "username": user.username or "",
            "first": now,
        }
        _daily()["new_users"] += 1
    db["users"][uid]["last"] = now
    save_db()


def track_download(platform: str | None) -> int:
    db["total_downloads"] += 1
    _daily()["downloads"] += 1
    if platform:
        db["platforms"][platform] = db["platforms"].get(platform, 0) + 1
    save_db()
    return db["total_downloads"]


def track_failure() -> None:
    db["total_failed"] += 1
    _daily()["failed"] += 1
    save_db()


def track_join() -> None:
    db["total_joins"] += 1
    _daily()["joins"] += 1
    save_db()

PLATFORM_META = {
    "youtube": ("📺", "یوتیوب"),
    "instagram": ("📸", "اینستاگرام"),
    "tiktok": ("🎵", "تیک‌تاک"),
    "pinterest": ("📌", "پینترست"),
}

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
        # Telegram raises "user not found" for people who are NOT members —
        # that IS the not-a-member signal, so block them.
        if "user not found" in str(exc).lower() or "participant_id_invalid" in str(exc).lower():
            return False
        logger.warning(
            "Membership check failed (%s). Is the bot an admin in %s?",
            exc, FORCE_JOIN_CHANNEL,
        )
        return True  # fail open on API/misconfig errors (see validate_force_join)


async def require_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    track_user(update.effective_user)
    if await is_member(context.bot, update.effective_user.id):
        return True
    if update.message:
        await update.message.reply_text(JOIN_REQUIRED, reply_markup=join_keyboard())
    elif update.callback_query:
        try:
            await update.callback_query.answer("❌ اول عضو کانال شو!", show_alert=True)
        except Exception:
            pass  # query may already have been answered
        try:
            await update.callback_query.message.reply_text(
                JOIN_REQUIRED, reply_markup=join_keyboard()
            )
        except Exception:
            pass
    return False

WELCOME = """
✨ <b>به Netora Downloader خوش اومدی!</b>

لینک بفرست، تحویل بگیر — همین‌قدر ساده ⚡

📺 <b>یوتیوب</b>  ·  کیفیت ۱۰۸۰ تا ۳۶۰ + 🎧 MP3
📸 <b>اینستاگرام</b>  ·  ریلز، ویدیو و کاروسل
🎵 <b>تیک‌تاک</b>  ·  ویدیو و آلبوم عکس
📌 <b>پینترست</b>  ·  ویدیو و عکس HD

🚀 همین حالا یک لینک بفرست!
"""

HELP = """
📖 <b>راهنمای استفاده</b>

۱. لینک پست رو کپی کن
۲. همین‌جا بفرست
۳. چند لحظه صبر کن تا فایل برسه ⏳

✨ <b>قابلیت‌ها:</b>
▫️ یوتیوب: کارت اطلاعات ویدیو + انتخاب کیفیت + MP3
▫️ تیک‌تاک و اینستا: دانلود خودکار، حتی پست‌های چندعکسی
▫️ نمایش عنوان، مدت و حجم فایل روی هر دانلود
"""


def detect_platform(url: str):
    for name, pattern in PLATFORMS:
        if pattern.search(url):
            return name
    return None


_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(text) -> str:
    return str(text).translate(_FA_DIGITS)


def fmt_duration(seconds) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    out = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return fa(out)


def fmt_size(num_bytes: float) -> str:
    mb = num_bytes / 1024 / 1024
    if mb >= 1024:
        return f"{fa(f'{mb / 1024:.1f}')} گیگابایت"
    if mb >= 1:
        return f"{fa(f'{mb:.0f}')} مگابایت"
    return f"{fa(f'{num_bytes / 1024:.0f}')} کیلوبایت"


def fmt_views(count) -> str:
    if not count:
        return ""
    if count >= 1_000_000:
        return f"{fa(f'{count / 1_000_000:.1f}')} میلیون بازدید"
    if count >= 1_000:
        return f"{fa(f'{count / 1_000:.0f}')} هزار بازدید"
    return f"{fa(count)} بازدید"


def progress_bar(pct: float) -> str:
    filled = min(10, int(pct // 10))
    return "█" * filled + "░" * (10 - filled)


async def safe_edit(message, text: str):
    try:
        await message.edit_text(text, parse_mode="HTML")
    except Exception:
        pass


def make_progress_hook(loop, status_message, label: str):
    state: dict = {}

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
                            f"⬇️ در حال دانلود از <b>{label}</b>...\n\n"
                            f"{progress_bar(pct)}  {fa(f'{pct:.0f}')}٪\n"
                            f"📦 {fmt_size(downloaded)} از {fmt_size(total)}",
                        ),
                        loop,
                    )
        elif d.get("status") == "finished":
            asyncio.run_coroutine_threadsafe(
                safe_edit(status_message, "⚙️ در حال پردازش نهایی..."), loop
            )

    return hook


# Verified against live YouTube (2026-08): the WEBPO clients below BOTH accept a
# bgutil PO token (required to get any format on a datacenter IP) AND are not
# subject to YouTube's SABR-only experiment, so they still return direct format
# URLs. web/mweb/web_safari/web_embedded are SABR-only now — yt-dlp skips their
# URL-less formats, which is exactly the "Requested format is not available"
# error, even with a valid PO token. So they are useless and no longer tried.
YT_CLIENTS_POT = ["tvhtml5", "web_remix", "tvhtml5_simply"]
# Cookieless clients that return direct URLs without a PO token — the fallback
# when the POT server isn't running (e.g. local dev) or the IP isn't flagged.
YT_CLIENTS_NOAUTH = ["android_vr", "android_testsuite", "android_producer"]
# bgutil PO-token provider (built into the Docker image): generates the tokens
# YouTube's web clients demand, which is what unlocks the formats behind
# "Requested format is not available". We run it as an in-process HTTP server
# (the project's recommended mode — cached, no per-call process spawn, and it
# keeps the plugin on the Node build instead of a Deno runtime that re-downloads
# its whole dependency tree on first use). Optional — absent locally, it's skipped.
BGUTIL_SERVER_HOME = os.environ.get(
    "BGUTIL_SERVER_HOME", "/opt/bgutil-ytdlp-pot-provider/server"
)
BGUTIL_MAIN_JS = Path(BGUTIL_SERVER_HOME, "build", "main.js")
POT_PORT = 4416
POT_BASE_URL = f"http://127.0.0.1:{POT_PORT}"
_pot_proc = None
_pot_ready = False


def _pot_server_available() -> bool:
    return shutil.which("node") is not None and BGUTIL_MAIN_JS.exists()


def start_pot_server() -> None:
    """Launch the bgutil POT HTTP server in the background and wait until it's
    actually listening. No-op when node or the built server isn't present
    (local dev) so YouTube just falls back to cookie-less/token-less clients."""
    global _pot_proc, _pot_ready
    if _pot_ready or not _pot_server_available():
        if not _pot_server_available():
            logger.info("bgutil POT server not present (node/build missing) — skipping")
        return
    try:
        # NOTE: the bgutil server only accepts --port; passing --host makes
        # commander reject the args and the process exits before ever binding.
        _pot_proc = subprocess.Popen(
            ["node", str(BGUTIL_MAIN_JS), "--port", str(POT_PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        logger.exception("Could not launch bgutil POT server")
        _pot_proc = None
        return
    for _ in range(40):  # up to ~20s for the server to bind
        if _pot_reachable():
            _pot_ready = True
            logger.info("bgutil POT server up on %s (pid %s)", POT_BASE_URL, _pot_proc.pid)
            return
        if _pot_proc.poll() is not None:
            logger.error("bgutil POT server exited early (code %s)", _pot_proc.returncode)
            _pot_proc = None
            return
        time.sleep(0.5)
    logger.error("bgutil POT server did not become ready in time")


def _pot_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", POT_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def pot_ok() -> bool:
    """Cheap liveness check: server was started and the process is still alive."""
    return _pot_ready and _pot_proc is not None and _pot_proc.poll() is None


def _stop_pot_server() -> None:
    if _pot_proc and _pot_proc.poll() is None:
        _pot_proc.terminate()


atexit.register(_stop_pot_server)


def _yt_args(clients) -> dict:
    args = {"youtube": {"player_client": clients}}
    if pot_ok():
        args["youtubepot-bgutilhttp"] = {"base_url": [POT_BASE_URL]}
    return args


def _with_auth(opts: dict, url: str = "") -> dict:
    has_cookies = Path(COOKIES_FILE).exists()
    if url and detect_platform(url) == "youtube":
        opts["extractor_args"] = _yt_args(YT_CLIENTS_POT)
        # The n/sig challenge needs a JS runtime. yt-dlp defaults to deno only,
        # so enable node (>= v22) too as a fallback.
        opts.setdefault("js_runtimes", {"deno": {}, "node": {}})
    if has_cookies:
        opts["cookiefile"] = COOKIES_FILE
    return opts


def _retryable_yt_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in (
        "requested format is not available",
        "sign in",
        "po token",
        "no video formats",
        "http error 403",
    ))


def _client_order(has_cookies: bool) -> list:
    # POT-backed non-SABR clients first (best chance on a flagged IP), cookieless
    # clients as the last resort.
    return (YT_CLIENTS_POT + YT_CLIENTS_NOAUTH if has_cookies
            else YT_CLIENTS_NOAUTH + YT_CLIENTS_POT)


def _run_ydl(opts: dict, url: str, dl: bool):
    """Run extract_info. A merged client list can hard-fail when one client gets
    bot-checked, hiding formats another client would serve — so on a retryable
    YouTube error, retry client-by-client (cookies preserved) and return the
    first that works."""
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=dl)
    except Exception as exc:
        if detect_platform(url) != "youtube" or not _retryable_yt_error(exc):
            raise
        has_cookies = "cookiefile" in opts
        logger.warning("YouTube primary attempt failed (%s) — trying clients one by one", exc)
        last = exc
        for client in _client_order(has_cookies):
            retry = dict(opts)
            retry["extractor_args"] = _yt_args([client])
            try:
                with yt_dlp.YoutubeDL(retry) as ydl:
                    info = ydl.extract_info(url, download=dl)
                logger.info("YouTube client %s succeeded", client)
                return info
            except Exception as e2:
                logger.warning("YouTube client %s failed: %s", client, e2)
                last = e2
        raise last


def extract_metadata(url: str):
    """Fetch title/thumbnail/etc. without downloading anything."""
    opts = _with_auth({
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
    }, url)
    return _run_ydl(opts, url, False)


def download(url: str, workdir: Path, status_message, loop,
             quality: int | None = None, audio: bool = False) -> tuple[list[Path], dict]:
    label = PLATFORM_META.get(detect_platform(url) or "", ("🔗", "لینک"))[1]
    opts = {
        "outtmpl": str(workdir / "%(title).100s-%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "retries": 3,
        "writethumbnail": False,
        "writesubtitles": False,
        "progress_hooks": [make_progress_hook(loop, status_message, label)],
    }
    # A TikTok photo post or IG carousel IS a playlist — download all items.
    # For YouTube keep the single video only, even if the URL has &list=.
    if detect_platform(url) == "youtube":
        opts["noplaylist"] = True
    else:
        opts["noplaylist"] = False
        opts["playlistend"] = 10
        # Skip failed downloads inside carousels, but still surface
        # extraction errors (login required, no video, ...) to the user.
        opts["ignoreerrors"] = "only_download"
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
            f"best[height<={quality}]/"
            f"bestvideo+bestaudio/"
            f"best"
        )
        opts["merge_output_format"] = "mp4"
    else:
        opts["format"] = (
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080]+bestaudio/"
            "best[ext=mp4]/"
            "bestvideo+bestaudio/"
            "best"
        )
        opts["merge_output_format"] = "mp4"

    _with_auth(opts, url)
    info = _run_ydl(opts, url, True)

    files = sorted(
        (p for p in workdir.iterdir() if p.is_file()
         and not p.name.endswith((".part", ".ytdl", ".frag"))),
        key=lambda p: p.stat().st_mtime,
    )
    return files, (info or {})


def fetch_og_image(url: str):
    import requests

    resp = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                          "Version/17.0 Mobile/15E148 Safari/604.1",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=20,
    )
    for pattern in (
        # Attribute order varies — allow anything between them, same tag only.
        r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"',
        r'<meta[^>]*content="([^"]+)"[^>]*property="og:image"',
        r'"image_xlarge_url"\s*:\s*"([^"]+)"',
        r'"image_large_url"\s*:\s*"([^"]+)"',
        r'"orig"\s*:\s*\{[^{}]*"url"\s*:\s*"([^"]+)"',
    ):
        m = re.search(pattern, resp.text)
        if m:
            # Pinterest embeds image URLs as JSON — slashes come escaped.
            found = m.group(1).replace("\\/", "/").replace("&amp;", "&")
            # i.pinimg.com serves a resized variant — grab the original.
            found = re.sub(r"i\.pinimg\.com/\d+x\d*/", "i.pinimg.com/originals/", found)
            return found
    logger.warning(
        "No image found in page %s (status %s, %d chars)",
        url, resp.status_code, len(resp.text),
    )
    return None


def build_caption(info: dict, url: str, size: int, bot_username: str) -> str:
    emoji, label = PLATFORM_META.get(detect_platform(url) or "", ("🔗", "لینک"))
    title = (info.get("title") or info.get("description") or "").strip()
    title = html.escape(re.sub(r"\s+", " ", title)[:120])

    lines = []
    if title:
        lines.append(f"🎬 <b>{title}</b>\n")
    parts = [f"{emoji} {label}"]
    if info.get("duration"):
        parts.append(f"⏱ {fmt_duration(info['duration'])}")
    if size:
        parts.append(f"📦 {fmt_size(size)}")
    lines.append("  │  ".join(parts))
    footer = f"🤖 @{bot_username}" if bot_username else ""
    if footer:
        lines.append(f"\n{footer}")
    return "\n".join(lines)


async def deliver(message, files: list[Path], caption: str = "",
                  thumbnail=None, title: str | None = None):
    images = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
    others = [f for f in files if f.suffix.lower() not in IMAGE_EXTS]

    if len(images) > 1:
        for i in range(0, len(images), 10):
            batch = images[i:i + 10]
            handles = [open(p, "rb") for p in batch]
            try:
                await message.reply_media_group([InputMediaPhoto(h) for h in handles])
            except Exception:
                # Telegram photos must be JPEG/PNG — send webp etc. as files.
                for h in handles:
                    h.seek(0)
                    await message.reply_document(h)
            finally:
                for h in handles:
                    h.close()
        if caption:
            await message.reply_text(caption, parse_mode="HTML")
    elif images:
        img = images[0]
        with open(img, "rb") as fh:
            try:
                await message.reply_photo(fh, caption=caption, parse_mode="HTML")
            except Exception:
                # Telegram photos must be JPEG/PNG — webp etc. go as documents.
                fh.seek(0)
                await message.reply_document(fh, caption=caption, parse_mode="HTML")

    for f in others:
        ext = f.suffix.lower()
        with open(f, "rb") as fh:
            if ext in VIDEO_EXTS:
                await message.reply_video(
                    fh, caption=caption, parse_mode="HTML",
                    supports_streaming=True, thumbnail=thumbnail,
                )
            elif ext in AUDIO_EXTS:
                await message.reply_audio(
                    fh, caption=caption, parse_mode="HTML",
                    thumbnail=thumbnail, title=title,
                )
            else:
                await message.reply_document(fh, caption=caption, parse_mode="HTML")


async def reply_image(message, image_url: str, caption: str):
    """Send an image by URL; fall back to a document if Telegram rejects it."""
    try:
        await message.reply_photo(image_url, caption=caption)
    except Exception:
        await message.reply_document(image_url, caption=caption)


async def process_download(message, url: str,
                           quality: int | None = None, audio: bool = False,
                           status_message=None):
    chat = message.chat
    emoji, label = PLATFORM_META.get(detect_platform(url) or "", ("🔗", "لینک"))
    status = status_message or await message.reply_text(f"{emoji} لینک {label} دریافت شد...")
    workdir = BASE_WORK_DIR / uuid.uuid4().hex[:12]
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        await chat.send_action(ChatAction.UPLOAD_VIDEO)
        loop = asyncio.get_running_loop()

        async with download_sem:
            await safe_edit(status, f"⬇️ در حال دانلود از {label}...")
            files, info = await asyncio.to_thread(
                download, url, workdir, status, loop, quality, audio
            )

        # Some pins/posts are plain images — yt-dlp can't handle them.
        if not files:
            image_url = await asyncio.to_thread(fetch_og_image, url)
            if image_url:
                await status.delete()
                track_download(detect_platform(url))
                await reply_image(
                    message, image_url,
                    f"{emoji} {label}  •  🖼 عکس\n\n🤖 {BOT_BRAND} • @{message.get_bot().username}",
                )
                return
            raise RuntimeError("nothing downloaded")

        ok = [f for f in files if f.stat().st_size <= MAX_FILE_SIZE]
        if not ok:
            track_failure()
            await safe_edit(
                status,
                f"⚠️ <b>فایل خیلی حجیمه!</b>\n\n"
                f"📦 حجم فایل: {fmt_size(max(f.stat().st_size for f in files))}\n"
                f"🚧 سقف مجاز: {fmt_size(MAX_FILE_SIZE)}\n\n"
                f"💡 برای یوتیوب: لینک رو دوباره بفرست و کیفیت پایین‌تر "
                f"یا MP3 رو انتخاب کن.",
            )
            return

        await safe_edit(status, "📤 در حال آپلود به تلگرام...")
        await chat.send_action(ChatAction.UPLOAD_VIDEO)
        download_no = track_download(detect_platform(url))
        total_size = sum(f.stat().st_size for f in ok)
        caption = build_caption(info, url, total_size, message.get_bot().username)
        caption += f"\n🔢 دانلود شماره {fa(download_no)}"
        await deliver(
            message, ok, caption=caption,
            thumbnail=info.get("thumbnail"), title=info.get("title"),
        )
        await status.delete()

    except Exception as exc:
        logger.exception("Download failed: %s", url)

        # Image-only pins/posts raise "no video" errors — grab the image.
        if "no video" in str(exc).lower():
            image_url = await asyncio.to_thread(fetch_og_image, url)
            if image_url:
                await status.delete()
                track_download(detect_platform(url))
                await reply_image(
                    message, image_url,
                    f"{emoji} {label}  •  🖼 عکس\n\n🤖 {BOT_BRAND} • @{message.get_bot().username}",
                )
                return
        track_failure()

        reason = re.sub(r"^ERROR:\s*", "", str(exc)).replace(url, "").strip()
        if len(reason) > 250:
            reason = reason[:250] + "…"
        hint = ""
        low = reason.lower()
        if "requested format" in low or "no video formats" in low:
            hint = (
                "\n\n💡 یوتیوب فرمتی برنگردوند — معمولاً یعنی PO-token provider "
                "روی سرور بالا نیومده یا IP سرور موقتاً فلگ شده. چند لحظه‌ی دیگه "
                "دوباره امتحان کن؛ اگه ادامه داشت به ادمین خبر بده."
            )
        elif "sign in" in low or "login" in low or "not a bot" in low:
            hint = (
                "\n\n💡 کوکی ست شده ولی یوتیوب قبولش نکرد — معمولاً یعنی **منقضی/باطل شده**. "
                "یه بار Sign out/Sign in کن، بلافاصله کوکی تازه رو اکسپورت کن و "
                "COOKIES_CONTENT رو آپدیت کن (بعد از اکسپورت، توی مرورگر روی یوتیوب "
                "کلیک نکن که کوکی rotate می‌شه)."
            )
        elif "instagram" in url:
            hint = "\n\n💡 بعضی پست‌های اینستاگرام نیاز به لاگین دارن — کوکی لازمه."
        await safe_edit(
            status,
            f"❌ <b>دانلود نشد</b>\n\n"
            f"🛠 <b>دلیل:</b>\n<code>{html.escape(reason) or 'خطای ناشناخته'}</code>{hint}\n\n"
            f"🔧 yt-dlp {yt_dlp.version.__version__}  •  "
            f"کوکی: {'✅' if Path(COOKIES_FILE).exists() else '❌'}",
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
        await send_youtube_card(update.message, url)
    else:
        await process_download(update.message, url)


def youtube_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 ۱۰۸۰", callback_data=f"yt:1080:{token}"),
            InlineKeyboardButton("🎬 ۷۲۰", callback_data=f"yt:720:{token}"),
            InlineKeyboardButton("🎬 ۴۸۰", callback_data=f"yt:480:{token}"),
            InlineKeyboardButton("🎬 ۳۶۰", callback_data=f"yt:360:{token}"),
        ],
        [InlineKeyboardButton("🎧 MP3 — فقط صوت", callback_data=f"yt:mp3:{token}")],
        [InlineKeyboardButton("❌ لغو", callback_data=f"yt:cancel:{token}")],
    ])


async def send_youtube_card(message, url: str):
    token = uuid.uuid4().hex[:8]
    pending[token] = url
    keyboard = youtube_keyboard(token)

    status = await message.reply_text("🔎 در حال دریافت اطلاعات ویدیو...")
    try:
        info = await asyncio.to_thread(extract_metadata, url)
    except Exception:
        # Metadata is a bonus — if it's blocked, still offer the buttons.
        await safe_edit(
            status,
            "🎬 <b>لینک یوتیوب دریافت شد!</b>\n\n👇 کیفیت موردنظرت رو انتخاب کن:",
        )
        try:
            await status.edit_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
        return

    title = html.escape((info.get("title") or "ویدیوی یوتیوب")[:150])
    card = f"🎬 <b>{title}</b>\n\n"
    details = []
    if info.get("uploader"):
        details.append(f"👤 {html.escape(info['uploader'])}")
    if info.get("duration"):
        details.append(f"⏱ {fmt_duration(info['duration'])}")
    if info.get("view_count"):
        details.append(f"👁 {fmt_views(info['view_count'])}")
    if details:
        card += "   ".join(details) + "\n\n"
    card += "👇 کیفیت موردنظرت رو انتخاب کن:"

    thumbnail = info.get("thumbnail")
    await status.delete()
    if thumbnail:
        try:
            await message.reply_photo(
                thumbnail, caption=card, parse_mode="HTML", reply_markup=keyboard
            )
            return
        except Exception:
            pass
    await message.reply_text(card, parse_mode="HTML", reply_markup=keyboard)


async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_membership(update, context):
        return
    try:
        _, q, token = query.data.split(":")
    except ValueError:
        return

    if q == "cancel":
        pending.pop(token, None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    url = pending.pop(token, None)
    if not url:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "⌛ این درخواست منقضی شده. لینک رو دوباره بفرست."
        )
        return

    # The card message may be a photo — remove buttons, then let
    # process_download create its own fresh status message.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    audio = q == "mp3"
    quality = None if audio else int(q)
    await process_download(query.message, url, quality=quality, audio=audio)


async def on_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    track_user(query.from_user)
    if await is_member(context.bot, query.from_user.id):
        track_join()
        await query.answer("✅ عضویت تأیید شد!")
        await safe_edit(query.message, "✅ عضویتت تأیید شد! حالا لینک رو بفرست 🚀")
    else:
        await query.answer("❌ هنوز عضو کانال نشدی!", show_alert=True)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=context.error)


# --- Admin panel -------------------------------------------------------------

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 بروزرسانی", callback_data="admin:refresh")],
        [InlineKeyboardButton("📢 ارسال پیام همگانی", callback_data="admin:broadcast")],
        [InlineKeyboardButton("💾 بکاپ آمار", callback_data="admin:backup"),
         InlineKeyboardButton("❌ بستن پنل", callback_data="admin:close")],
    ])


def admin_panel_text() -> str:
    daily = db["daily"].get(today_str(), {})
    lines = [
        "🛠 <b>پنل مدیریت</b>\n",
        f"👥 <b>کاربران:</b> {fa(len(db['users']))} نفر"
        f"  (امروز: {fa(daily.get('new_users', 0))})",
        f"⬇️ <b>دانلودها:</b> {fa(db['total_downloads'])}"
        f"  (امروز: {fa(daily.get('downloads', 0))})",
        f"❌ <b>ناموفق:</b> {fa(db['total_failed'])}"
        f"  (امروز: {fa(daily.get('failed', 0))})",
        f"✅ <b>عضویت کانال:</b> {fa(db['total_joins'])}"
        f"  (امروز: {fa(daily.get('joins', 0))})",
    ]
    if db["platforms"]:
        lines.append("\n📊 <b>دانلود بر اساس پلتفرم:</b>")
        for key, (emoji, label) in PLATFORM_META.items():
            if db["platforms"].get(key):
                lines.append(f"{emoji} {label}: {fa(db['platforms'][key])}")
    uptime = fmt_duration(time.time() - START_TIME)
    lines.append(
        f"\n⚙️ آپتایم: {uptime}  │  yt-dlp {yt_dlp.version.__version__}"
        f"  │  کوکی: {'✅' if Path(COOKIES_FILE).exists() else '❌'}"
    )
    return "\n".join(lines)


async def on_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user)
    if update.effective_user.id not in ADMIN_IDS:
        return  # silent — don't reveal the panel exists
    await update.message.reply_text(
        admin_panel_text(), parse_mode="HTML", reply_markup=admin_keyboard()
    )


async def on_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("⛔ دسترسی نداری!", show_alert=True)
        return
    action = query.data.split(":")[1]
    if action == "refresh":
        await query.answer("🔄 بروز شد")
        await safe_edit(query.message, admin_panel_text())
    elif action == "broadcast":
        context.user_data["awaiting_broadcast"] = True
        await query.answer()
        await query.message.reply_text(
            "📢 پیام همگانی رو بفرست — متن، عکس، ویدیو... هر چی بفرستی "
            "برای همه کاربران کپی می‌شه.\n\nبرای انصراف: /cancel"
        )
    elif action == "backup":
        await query.answer()
        path = Path(DATA_FILE)
        if not path.exists():
            await query.answer("⚠️ هنوز فایل آماری ساخته نشده.", show_alert=True)
            return
        with path.open("rb") as fh:
            await query.message.reply_document(
                document=fh,
                filename="bot_data.json",
                caption="💾 بکاپ آمار — برای انتقال به هاست جدید، این فایل رو "
                        "توی مسیر DATA_FILE اونجا کپی کن و ربات رو ری‌استارت کن.",
            )
    elif action == "close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.pop("awaiting_broadcast", False):
        await update.message.reply_text("❌ ارسال همگانی لغو شد.")


async def on_maybe_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs in group -1, before the link handler. Consumes the admin's message
    when a broadcast is pending so it isn't treated as a download link."""
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        return
    if not context.user_data.pop("awaiting_broadcast", False):
        return
    msg = update.effective_message
    if not db["users"]:
        await msg.reply_text("⚠️ هنوز هیچ کاربری ربات رو استارت نکرده.")
        raise ApplicationHandlerStop
    status = await msg.reply_text(f"📢 در حال ارسال به {fa(len(db['users']))} کاربر...")
    ok = fail = 0
    for uid in list(db["users"]):
        try:
            await context.bot.copy_message(
                chat_id=int(uid), from_chat_id=msg.chat_id, message_id=msg.message_id
            )
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # stay under Telegram rate limits
    await safe_edit(
        status,
        f"📢 <b>ارسال همگانی تمام شد</b>\n\n"
        f"✅ موفق: {fa(ok)}\n"
        f"❌ ناموفق (بلاک/حذف ربات): {fa(fail)}",
    )
    raise ApplicationHandlerStop


async def validate_force_join(app: Application):
    """Surface force-join misconfig loudly at startup instead of the
    membership check silently failing open for everyone."""
    if not FORCE_JOIN_CHANNEL:
        return
    try:
        await app.bot.get_chat(FORCE_JOIN_CHANNEL)
        me = await app.bot.get_chat_member(FORCE_JOIN_CHANNEL, app.bot.id)
        if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            logger.error(
                "FORCE JOIN: the bot is NOT an admin in %s — membership checks "
                "will silently PASS for everyone! Add the bot as an admin.",
                FORCE_JOIN_CHANNEL,
            )
        else:
            logger.info("Force-join verified — bot is admin in %s", FORCE_JOIN_CHANNEL)
    except Exception as exc:
        logger.error(
            "FORCE JOIN: cannot access %s (%s) — membership checks will "
            "silently PASS for everyone! Fix: the bot must be an ADMIN in the "
            "channel and FORCE_JOIN_CHANNEL must be @username or a numeric id.",
            FORCE_JOIN_CHANNEL, exc,
        )


def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ متغیر محیطی BOT_TOKEN تنظیم نشده!")

    # Bring up the bgutil PO-token server BEFORE polling starts — without it the
    # youtubepot-bgutilhttp extractor arg is never added and YouTube's web
    # clients fail with "Requested format is not available".
    start_pot_server()

    builder = Application.builder().token(BOT_TOKEN)
    if BOT_API_URL:
        builder = (
            builder
            .base_url(f"{BOT_API_URL}/bot")
            .base_file_url(f"{BOT_API_URL}/file/bot")
            .read_timeout(600)
            .write_timeout(600)
            .connect_timeout(60)
        )
        logger.info("Using local Bot API server at %s (max upload %d MB)",
                    BOT_API_URL, MAX_FILE_SIZE // 1024 // 1024)
    app = builder.build()
    app.post_init = validate_force_join
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CommandHandler("admin", on_admin))
    app.add_handler(CommandHandler("cancel", on_cancel))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^yt:"))
    app.add_handler(CallbackQueryHandler(on_join_check, pattern=r"^join:check$"))
    app.add_handler(CallbackQueryHandler(on_admin_cb, pattern=r"^admin:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(
        MessageHandler(~filters.COMMAND & ~filters.StatusUpdate.ALL, on_maybe_broadcast),
        group=-1,
    )
    app.add_error_handler(on_error)

    logger.info(
        "Bot is starting... (yt-dlp %s, PO-token provider: %s, cookies: %s)",
        yt_dlp.version.__version__,
        "✅" if pot_ok() else "❌",
        "✅" if Path(COOKIES_FILE).exists() else "❌",
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
