from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import CHANNEL_LINK

BTN_CHAT = "💬 چت با هوش مصنوعی"
BTN_IMAGE = "🖼 ساخت تصویر"
BTN_ACCOUNT = "👤 حساب من"
BTN_TOP = "🏆 برترین معرف‌ها"
BTN_NEWCHAT = "🆕 گفتگوی جدید"
BTN_HELP = "ℹ️ راهنما"


def join_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if CHANNEL_LINK:
        kb.button(text="📢 عضویت در چنل", url=CHANNEL_LINK)
    kb.button(text="✅ عضو شدم، بررسی کن", callback_data="check_access")
    kb.adjust(1)
    return kb.as_markup()


def refer_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 بررسی دسترسی", callback_data="check_access")
    return kb.as_markup()


def menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CHAT), KeyboardButton(text=BTN_IMAGE)],
            [KeyboardButton(text=BTN_ACCOUNT), KeyboardButton(text=BTN_TOP)],
            [KeyboardButton(text=BTN_NEWCHAT), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def ratio_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬛️ مربع (1:1)", callback_data="img:1:1")
    kb.button(text="🖥 افقی (16:9)", callback_data="img:16:9")
    kb.button(text="📱 عمودی (9:16)", callback_data="img:9:16")
    kb.adjust(1)
    return kb.as_markup()


def admin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 آمار ربات", callback_data="admin:stats")
    kb.button(text="👤 اطلاعات کاربر", callback_data="admin:userinfo")
    kb.button(text="🏆 برترین معرف‌ها", callback_data="admin:top")
    kb.button(text="📢 پیام همگانی", callback_data="admin:broadcast")
    kb.button(text="⚙️ تنظیمات", callback_data="admin:settings")
    kb.button(text="🚫 بن کاربر", callback_data="admin:ban")
    kb.button(text="✅ رفع بن", callback_data="admin:unban")
    kb.button(text="📥 بکاپ دیتابیس", callback_data="admin:backup")
    kb.button(text="📤 ریستور بکاپ", callback_data="admin:restore")
    kb.adjust(2, 2, 2, 1, 2)
    return kb.as_markup()


def admin_settings_kb(maintenance: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 تعداد معرفی لازم", callback_data="admin:setrefs")
    kb.button(text="💬 سقف چت روزانه", callback_data="admin:setchat")
    kb.button(text="🖼 سقف تصویر روزانه", callback_data="admin:setimage")
    kb.button(
        text=f"🔧 حالت تعمیرات: {'🟢 روشن' if maintenance else '🔴 خاموش'}",
        callback_data="admin:maintenance",
    )
    kb.button(text="🔙 بازگشت به پنل", callback_data="admin:panel")
    kb.adjust(2, 1, 1, 1)
    return kb.as_markup()
