import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import database as db
from config import ADMIN_IDS, AUTO_BACKUP, BACKUP_HOUR, BOT_TOKEN
from handlers import admin, user
from utils import send_backup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def auto_backup_loop(bot: Bot):
    """هر روز سر ساعت مشخص بکاپ را برای ادمین‌ها می‌فرستد."""
    while True:
        now = datetime.now()
        target = now.replace(hour=BACKUP_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            for admin_id in ADMIN_IDS:
                try:
                    await send_backup(bot, admin_id, auto=True)
                except Exception:
                    logger.exception("Auto-backup failed for admin %s", admin_id)
        except Exception:
            logger.exception("Auto-backup failed")


async def main():
    await db.init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin.router)
    dp.include_router(user.router)
    if AUTO_BACKUP and ADMIN_IDS:
        asyncio.create_task(auto_backup_loop(bot))
        logger.info("Auto-backup enabled, daily at %02d:00 server time", BACKUP_HOUR)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
