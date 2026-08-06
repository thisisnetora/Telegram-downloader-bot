import asyncio
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import database as db
from config import ADMIN_IDS, BOT_NAME, DB_PATH
from keyboards import admin_kb, admin_settings_kb
from utils import send_backup

router = Router()


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        return getattr(event.from_user, "id", None) in ADMIN_IDS


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminForm(StatesGroup):
    broadcast = State()
    setrefs = State()
    setchat = State()
    setimage = State()
    ban = State()
    unban = State()
    userinfo = State()
    restore = State()


async def show_panel(message: Message):
    await message.answer(
        f"🛠 <b>پنل مدیریت {BOT_NAME}</b>\n\nیکی از گزینه‌ها رو انتخاب کن 👇",
        reply_markup=admin_kb(),
    )


@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    await state.clear()
    await show_panel(message)


@router.callback_query(F.data == "admin:panel")
async def cb_panel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_panel(callback.message)
    await callback.answer()


@router.callback_query(F.data == "admin:stats")
async def cb_stats(callback: CallbackQuery):
    s = await db.stats()
    await callback.message.answer(
        "📊 <b>آمار کامل ربات</b>\n\n"
        "<blockquote>"
        f"👤 کل کاربران: <b>{s['total']}</b>\n"
        f"🆕 کاربران امروز: <b>{s['today']}</b>\n"
        f"🔓 کاربران فعال‌شده: <b>{s['unlocked']}</b>\n"
        f"🚫 مسدودشده: <b>{s['banned']}</b>"
        "</blockquote>"
        "<blockquote>"
        f"💬 چت امروز / کل: <b>{s['chat_today']}</b> / <b>{s['chat_total']}</b>\n"
        f"🖼 تصویر امروز / کل: <b>{s['image_today']}</b> / <b>{s['image_total']}</b>"
        "</blockquote>"
        f"⚙️ معرفی لازم: <b>{s['required']}</b>",
        reply_markup=admin_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:top")
async def cb_top(callback: CallbackQuery):
    top = await db.top_referrers(10)
    if not top:
        await callback.message.answer("🏆 هنوز هیچ معرفی ثبت نشده.", reply_markup=admin_kb())
        return await callback.answer()
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, row in enumerate(top):
        rank = medals[i] if i < 3 else f"<b>{i + 1}.</b>"
        name = escape(row["full_name"] or "کاربر")
        uname = f" (@{row['username']})" if row["username"] else ""
        lines.append(f"{rank} {name}{uname} — <b>{row['c']}</b> دعوت")
    await callback.message.answer(
        "🏆 <b>برترین معرف‌ها</b>\n\n" + "\n".join(lines),
        reply_markup=admin_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:userinfo")
async def cb_userinfo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.userinfo)
    await callback.message.answer("👤 آیدی عددی کاربر رو بفرست:")
    await callback.answer()


@router.message(AdminForm.userinfo)
async def do_userinfo(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ آیدی باید یک عدد باشه.")
        return
    await state.clear()
    user = await db.get_user(int(message.text))
    if not user:
        await message.answer("❌ کاربری با این آیدی پیدا نشد.", reply_markup=admin_kb())
        return
    count = await db.referral_count(user["user_id"])
    usage = await db.usage_today(user["user_id"])
    username = f"@{user['username']}" if user["username"] else "—"
    await message.answer(
        "👤 <b>اطلاعات کاربر</b>\n\n"
        "<blockquote>"
        f"🪪 نام: <b>{escape(user['full_name'])}</b>\n"
        f"🆔 آیدی: <code>{user['user_id']}</code>\n"
        f"🔗 یوزرنیم: {username}\n"
        f"📅 عضویت: {user['created_at'][:10]}\n"
        f"👥 معرفی‌ها: <b>{count}</b>\n"
        f"💬 چت امروز: {usage['chat']} | 🖼 تصویر امروز: {usage['image']}\n"
        f"🚫 وضعیت: {'مسدود ⛔️' if user['is_banned'] else 'فعال ✅'}"
        "</blockquote>",
        reply_markup=admin_kb(),
    )


@router.callback_query(F.data == "admin:broadcast")
async def cb_broadcast(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.broadcast)
    await callback.message.answer("📢 پیامی که می‌خوای برای همه کاربران ارسال بشه رو بفرست (متن، عکس، ویدیو و...):")
    await callback.answer()


@router.message(AdminForm.broadcast)
async def do_broadcast(message: Message, state: FSMContext):
    await state.clear()
    user_ids = await db.all_user_ids()
    await message.answer(f"⏳ در حال ارسال به <b>{len(user_ids)}</b> کاربر...")

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await message.copy_to(uid)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await message.answer(
        "✅ <b>ارسال همگانی تمام شد</b>\n\n"
        f"📨 موفق: <b>{sent}</b>\n"
        f"❌ ناموفق: <b>{failed}</b>",
        reply_markup=admin_kb(),
    )


@router.callback_query(F.data == "admin:settings")
async def cb_settings(callback: CallbackQuery):
    maintenance = await db.maintenance_on()
    required = await db.required_referrals()
    chat_limit = await db.daily_chat_limit()
    image_limit = await db.daily_image_limit()
    await callback.message.answer(
        "⚙️ <b>تنظیمات ربات</b>\n\n"
        "<blockquote>"
        f"👥 تعداد معرفی لازم: <b>{required}</b>\n"
        f"💬 سقف چت روزانه: <b>{chat_limit}</b>\n"
        f"🖼 سقف تصویر روزانه: <b>{image_limit}</b>\n"
        f"🔧 حالت تعمیرات: <b>{'روشن 🟢' if maintenance else 'خاموش 🔴'}</b>"
        "</blockquote>",
        reply_markup=admin_settings_kb(maintenance),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:maintenance")
async def cb_maintenance(callback: CallbackQuery):
    current = await db.maintenance_on()
    await db.set_setting("maintenance", "0" if current else "1")
    now_on = not current
    await callback.message.edit_reply_markup(reply_markup=admin_settings_kb(now_on))
    await callback.answer(f"🔧 حالت تعمیرات {'روشن شد 🟢' if now_on else 'خاموش شد 🔴'}")


async def ask_number(callback: CallbackQuery, state: FSMContext, form_state, title: str, current: int):
    await state.set_state(form_state)
    await callback.message.answer(f"⚙️ {title} فعلی: <b>{current}</b>\n\nعدد جدید رو بفرست:")
    await callback.answer()


async def save_number(message: Message, state: FSMContext, key: str, title: str):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ لطفاً یک عدد صحیح غیرمنفی بفرست.")
        return
    await state.clear()
    await db.set_setting(key, message.text)
    await message.answer(f"✅ {title} روی <b>{message.text}</b> تنظیم شد.", reply_markup=admin_kb())


@router.callback_query(F.data == "admin:setrefs")
async def cb_setrefs(callback: CallbackQuery, state: FSMContext):
    await ask_number(callback, state, AdminForm.setrefs, "تعداد معرفی لازم", await db.required_referrals())


@router.message(AdminForm.setrefs)
async def do_setrefs(message: Message, state: FSMContext):
    await save_number(message, state, "required_referrals", "تعداد معرفی لازم")


@router.callback_query(F.data == "admin:setchat")
async def cb_setchat(callback: CallbackQuery, state: FSMContext):
    await ask_number(callback, state, AdminForm.setchat, "سقف چت روزانه", await db.daily_chat_limit())


@router.message(AdminForm.setchat)
async def do_setchat(message: Message, state: FSMContext):
    await save_number(message, state, "daily_chat_limit", "سقف چت روزانه")


@router.callback_query(F.data == "admin:setimage")
async def cb_setimage(callback: CallbackQuery, state: FSMContext):
    await ask_number(callback, state, AdminForm.setimage, "سقف تصویر روزانه", await db.daily_image_limit())


@router.message(AdminForm.setimage)
async def do_setimage(message: Message, state: FSMContext):
    await save_number(message, state, "daily_image_limit", "سقف تصویر روزانه")


@router.callback_query(F.data == "admin:ban")
async def cb_ban(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.ban)
    await callback.message.answer("🚫 آیدی عددی کاربری که می‌خوای بن بشه رو بفرست:")
    await callback.answer()


@router.message(AdminForm.ban)
async def do_ban(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ آیدی باید یک عدد باشه.")
        return
    await state.clear()
    await db.set_ban(int(message.text), True)
    await message.answer(f"🚫 کاربر <code>{message.text}</code> بن شد.", reply_markup=admin_kb())


@router.callback_query(F.data == "admin:unban")
async def cb_unban(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.unban)
    await callback.message.answer("✅ آیدی عددی کاربری که می‌خوای رفع بن بشه رو بفرست:")
    await callback.answer()


@router.callback_query(F.data == "admin:backup")
async def cb_backup(callback: CallbackQuery):
    await callback.answer("⏳ در حال آماده‌سازی بکاپ...")
    try:
        await send_backup(callback.bot, callback.message.chat.id)
    except Exception as e:
        await callback.message.answer(f"⚠️ بکاپ گرفتن با خطا مواجه شد:\n<code>{escape(str(e))}</code>")


@router.callback_query(F.data == "admin:restore")
async def cb_restore(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.restore)
    await callback.message.answer(
        "📤 <b>ریستور بکاپ</b>\n\n"
        "فایل بکاپ (<code>.db</code>) که قبلاً از ربات گرفتی رو همین‌جا بفرست.\n\n"
        "⚠️ <b>توجه:</b> دیتابیس فعلی کاملاً با فایل بکاپ جایگزین می‌شه!"
    )
    await callback.answer()


@router.message(AdminForm.restore, F.document)
async def do_restore(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not doc.file_name or not doc.file_name.endswith(".db"):
        await message.answer("❌ فایل باید پسوند <code>.db</code> داشته باشه. دوباره بفرست:")
        return

    await message.answer("⏳ در حال دانلود و بررسی فایل...")
    try:
        file = await bot.get_file(doc.file_id)
        buffer = await bot.download_file(file.file_path)
        content = buffer.read()
    except Exception:
        await message.answer("⚠️ دانلود فایل با خطا مواجه شد. دوباره تلاش کن.")
        return

    if not db.is_valid_backup(content):
        await message.answer("❌ این فایل یک دیتابیس معتبر نیست. فایل درست رو بفرست:")
        return

    await state.clear()
    with open(DB_PATH, "wb") as f:
        f.write(content)
    await db.init_db()  # اطمینان از سالم بودن ساختار

    s = await db.stats()
    await message.answer(
        "✅ <b>بکاپ با موفقیت ریستور شد!</b>\n\n"
        f"👤 کاربران: <b>{s['total']}</b> | 🔓 فعال‌شده: <b>{s['unlocked']}</b>\n"
        "ربات حالا با دیتای قبلی کار می‌کنه 🎉",
        reply_markup=admin_kb(),
    )


@router.message(AdminForm.unban)
async def do_unban(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ آیدی باید یک عدد باشه.")
        return
    await state.clear()
    await db.set_ban(int(message.text), False)
    await message.answer(f"✅ کاربر <code>{message.text}</code> رفع بن شد.", reply_markup=admin_kb())
