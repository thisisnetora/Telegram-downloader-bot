import time
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, URLInputFile

import database as db
from config import ADMIN_IDS, BOT_NAME
from keyboards import (
    BTN_ACCOUNT,
    BTN_CHAT,
    BTN_HELP,
    BTN_IMAGE,
    BTN_NEWCHAT,
    BTN_TOP,
    join_kb,
    menu_kb,
    ratio_kb,
    refer_kb,
)
from services import ai
from utils import answer_long, evaluate_access

router = Router()


class Form(StatesGroup):
    image = State()


_last_action: dict[int, float] = {}


def is_flood(user_id: int) -> bool:
    now = time.monotonic()
    if now - _last_action.get(user_id, 0) < 1.2:
        return True
    _last_action[user_id] = now
    return False


async def referral_link(bot: Bot, user_id: int) -> str:
    me = await bot.get_me()
    return f"https://t.me/{me.username}?start=ref_{user_id}"


async def send_gate(message: Message, bot: Bot):
    """بر اساس وضعیت کاربر پیام مناسب (عضویت/معرفی/منو) را می‌فرستد."""
    user_id = message.from_user.id
    name = escape(message.from_user.first_name or "دوست عزیز")
    status = await evaluate_access(bot, user_id)

    if status == "banned":
        await message.answer("🚫 متأسفانه حساب شما توسط مدیریت مسدود شده است.")
        return

    if status == "maintenance":
        await message.answer(
            f"🛠 <b>{BOT_NAME} در حال به‌روزرسانی است</b>\n\n"
            "کمی بعد برگرد، برمی‌گردیم قوی‌تر از قبل! 💪"
        )
        return

    if status == "join":
        await message.answer(
            f"👋 <b>سلام {name}!</b>\n"
            f"به <b>{BOT_NAME}</b> خوش اومدی 🤖✨\n\n"
            "<blockquote>"
            "🧠 گفتگو با هوش مصنوعی پیشرفته\n"
            "🎨 ساخت تصویر از روی متن\n"
            "🎁 کاملاً رایگان"
            "</blockquote>\n"
            "برای شروع، اول باید عضو چنل ما بشی 👇\n"
            "بعد روی «✅ عضو شدم» بزن.",
            reply_markup=join_kb(),
        )
        return

    if status == "refer":
        required = await db.required_referrals()
        count = await db.referral_count(user_id)
        link = await referral_link(bot, user_id)
        bar = "🟩" * count + "⬜️" * (required - count)
        await message.answer(
            "✅ <b>عضویتت در چنل تأیید شد!</b>\n\n"
            f"حالا فقط کافیه <b>{required}</b> نفر رو با لینک اختصاصیت دعوت کنی تا ربات برات فعال بشه "
            "<i>(کسانی که با لینک تو وارد بشن و عضو چنل بشن شمرده می‌شن)</i>\n\n"
            f"🔗 <b>لینک اختصاصی تو:</b>\n<code>{link}</code>\n\n"
            f"{bar}  <b>{count}</b> از <b>{required}</b>",
            reply_markup=refer_kb(),
        )
        return

    await message.answer(
        f"🎉 <b>{name} عزیز، حسابت در {BOT_NAME} فعاله!</b>\n\n"
        "<blockquote>"
        "💬 پیامت رو بنویس تا هوش مصنوعی جواب بده\n"
        "🖼 روی «ساخت تصویر» بزن و تصویر دلخواهت رو بساز"
        "</blockquote>",
        reply_markup=menu_kb(),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot, state: FSMContext):
    await state.clear()

    referrer = None
    if command.args and command.args.startswith("ref_"):
        try:
            referrer = int(command.args[4:])
        except ValueError:
            referrer = None
        if referrer == message.from_user.id:
            referrer = None

    await db.add_user(message.from_user.id, message.from_user.username, message.from_user.full_name, referrer)
    await send_gate(message, bot)


@router.callback_query(F.data == "check_access")
async def cb_check_access(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await db.add_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name, None)
    await callback.answer("⏳ در حال بررسی...")
    await send_gate(callback.message, bot)


async def ensure_access(message: Message, bot: Bot) -> bool:
    """True اگر کاربر دسترسی دارد؛ در غیر این صورت پیام دروازه را می‌فرستد."""
    if await evaluate_access(bot, message.from_user.id) != "ok":
        await send_gate(message, bot)
        return False
    return True


@router.message(F.text == BTN_CHAT)
async def chat_mode(message: Message, bot: Bot, state: FSMContext):
    if not await ensure_access(message, bot):
        return
    await state.clear()
    await message.answer(
        "💬 <b>حالت گفتگو فعاله</b>\n\nهر سوالی داری بنویس تا جواب بدم! ✨",
        reply_markup=menu_kb(),
    )


@router.message(F.text == BTN_IMAGE)
async def image_mode(message: Message, bot: Bot, state: FSMContext):
    if not await ensure_access(message, bot):
        return
    await message.answer(
        "🖼 <b>ساخت تصویر با هوش مصنوعی</b>\n\nاول قاب تصویرت رو انتخاب کن 👇",
        reply_markup=ratio_kb(),
    )


@router.callback_query(F.data.startswith("img:"))
async def cb_pick_ratio(callback: CallbackQuery, state: FSMContext):
    ratio = callback.data.split(":", 1)[1]
    await state.set_state(Form.image)
    await state.update_data(ratio=ratio)
    label = {"1:1": "⬛️ مربع", "16:9": "🖥 افقی", "9:16": "📱 عمودی"}.get(ratio, ratio)
    await callback.message.answer(
        f"✅ قاب <b>{label}</b> انتخاب شد.\n\n"
        "حالا توصیف تصویری که می‌خوای رو بنویس 🎨\n"
        "<i>(انگلیسی بنویسی نتیجه خیلی بهتره)</i>\n\n"
        "مثال: <code>a cute cat astronaut on the moon, digital art</code>"
    )
    await callback.answer()


@router.message(F.text == BTN_ACCOUNT)
async def my_account(message: Message, bot: Bot):
    if not await ensure_access(message, bot):
        return
    user_id = message.from_user.id
    required = await db.required_referrals()
    count = await db.referral_count(user_id)
    usage = await db.usage_today(user_id)
    chat_limit = await db.daily_chat_limit()
    image_limit = await db.daily_image_limit()
    link = await referral_link(bot, user_id)
    await message.answer(
        "👤 <b>حساب کاربری تو</b>\n\n"
        f"👥 دعوت‌شده‌ها: <b>{count}</b> نفر <i>(حداقل فعال‌سازی: {required})</i>\n"
        f"💬 چت امروز: <b>{usage['chat']}</b> از <b>{chat_limit}</b>\n"
        f"🖼 تصویر امروز: <b>{usage['image']}</b> از <b>{image_limit}</b>\n\n"
        f"🔗 <b>لینک دعوت اختصاصی تو:</b>\n<code>{link}</code>\n\n"
        "<i>هرچی بیشتر دعوت کنی، تو جدول برترین‌ها بالاتر میای! 🏆</i>"
    )


@router.message(F.text == BTN_TOP)
async def top_referrers(message: Message, bot: Bot):
    if not await ensure_access(message, bot):
        return
    top = await db.top_referrers(10)
    if not top:
        await message.answer("🏆 هنوز کسی کسی رو دعوت نکرده!\n\nاولین نفر باش و ببرش بالا 🚀")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, row in enumerate(top):
        rank = medals[i] if i < 3 else f"<b>{i + 1}.</b>"
        name = escape(row["full_name"] or "کاربر")
        lines.append(f"{rank} {name} — <b>{row['c']}</b> دعوت")
    await message.answer(
        "🏆 <b>برترین معرف‌های ربات</b>\n\n" + "\n".join(lines) + "\n\n<i>تو هم می‌تونی تو این لیست باشی! 👤 حساب من ← لینک دعوتت رو بگیر</i>"
    )


@router.message(F.text == BTN_NEWCHAT)
async def new_chat(message: Message, bot: Bot, state: FSMContext):
    if not await ensure_access(message, bot):
        return
    await state.clear()
    ai.clear_history(message.from_user.id)
    await message.answer(
        "🆕 <b>گفتگوی جدید شروع شد!</b>\n\nحافظه گفتگوی قبلی پاک شد. هرچی می‌خوای بپرس 💬",
        reply_markup=menu_kb(),
    )


HELP_TEXT = (
    f"ℹ️ <b>راهنمای {BOT_NAME}</b>\n\n"
    "<blockquote>"
    "💬 <b>چت با هوش مصنوعی:</b> کافیه پیامت رو بنویسی؛ ربات گفتگو رو به خاطر می‌سپره\n"
    "🖼 <b>ساخت تصویر:</b> قاب رو انتخاب کن، توصیف تصویر رو بنویس (انگلیسی بهتره)\n"
    "🆕 <b>گفتگوی جدید:</b> حافظه چت رو پاک می‌کنه\n"
    "👤 <b>حساب من:</b> لینک دعوت، پیشرفت و مصرف امروزت"
    "</blockquote>\n"
    "⏳ ساخت تصویر ممکنه چند ثانیه طول بکشه، صبور باش!\n"
    "📵 هر کاربر روزانه سقف مشخصی چت و تصویر داره."
)


@router.message(F.text == BTN_HELP)
@router.message(Command("help"))
async def help_cmd(message: Message, bot: Bot):
    if not await ensure_access(message, bot):
        return
    await message.answer(HELP_TEXT)


@router.message(F.text)
async def handle_text(message: Message, bot: Bot, state: FSMContext):
    if is_flood(message.from_user.id):
        return
    if not await ensure_access(message, bot):
        return

    user_id = message.from_user.id
    is_admin = user_id in ADMIN_IDS

    if await state.get_state() == Form.image:
        usage = await db.usage_today(user_id)
        limit = await db.daily_image_limit()
        if not is_admin and usage["image"] >= limit:
            await message.answer(
                f"📵 <b>به سقف روزانه ساخت تصویر ({limit} تصویر) رسیدی!</b>\n\n"
                "فردا دوباره در خدمتیم 🌙"
            )
            return

        data = await state.get_data()
        ratio = data.get("ratio", "1:1")
        status_msg = await message.answer("🎨 <b>در حال ساخت تصویر...</b>\n\n⏳ معمولاً تا چند ثانیه طول می‌کشه")
        await bot.send_chat_action(message.chat.id, "upload_photo")
        try:
            photo = URLInputFile(ai.image_url(message.text, ratio), filename="image.jpg")
            await message.answer_photo(
                photo,
                caption=f"🖼 <b>تصویرت آماده‌ست!</b>\n\n📝 <i>{escape(message.text[:200])}</i>",
            )
            await db.increment_usage(user_id, "image")
            await status_msg.delete()
        except Exception:
            await status_msg.edit_text("⚠️ ساخت تصویر با خطا مواجه شد. دوباره تلاش کن یا توصیف ساده‌تری بنویس.")
        return

    if len(message.text) > 2000:
        await message.answer("✂️ پیامت خیلی طولانیه! لطفاً کوتاه‌تر بنویس (حداکثر ۲۰۰۰ کاراکتر).")
        return

    usage = await db.usage_today(user_id)
    limit = await db.daily_chat_limit()
    if not is_admin and usage["chat"] >= limit:
        await message.answer(
            f"📵 <b>به سقف روزانه چت ({limit} پیام) رسیدی!</b>\n\n"
            "فردا دوباره در خدمتیم 🌙"
        )
        return

    await bot.send_chat_action(message.chat.id, "typing")
    reply = await ai.gemini_chat(user_id, message.text)
    await db.increment_usage(user_id, "chat")
    await answer_long(message, reply)
