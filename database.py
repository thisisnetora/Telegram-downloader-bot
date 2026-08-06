import aiosqlite

from config import (
    DB_PATH,
    DEFAULT_DAILY_CHAT_LIMIT,
    DEFAULT_DAILY_IMAGE_LIMIT,
    DEFAULT_REQUIRED_REFERRALS,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    referrer_id INTEGER,
    referral_credited INTEGER DEFAULT 0,
    is_banned INTEGER DEFAULT 0,
    unlock_announced INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER,
    day TEXT,
    chat_count INTEGER DEFAULT 0,
    image_count INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
"""

# برای دیتابیس‌های قدیمی که این ستون را ندارند
_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN unlock_announced INTEGER DEFAULT 0",
]


async def _connect():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    db = await _connect()
    try:
        await db.executescript(_SCHEMA)
        for migration in _MIGRATIONS:
            try:
                await db.execute(migration)
            except Exception:
                pass
        await db.commit()
    finally:
        await db.close()


async def create_backup(backup_path: str):
    """اسنپ‌شات سازگار از دیتابیس با VACUUM INTO می‌گیرد."""
    db = await aiosqlite.connect(DB_PATH, isolation_level=None)  # VACUUM نباید داخل تراکنش باشد
    try:
        await db.execute("VACUUM INTO ?", (backup_path,))
    finally:
        await db.close()


def is_valid_backup(data: bytes) -> bool:
    return data.startswith(b"SQLite format 3")


async def add_user(user_id: int, username: str | None, full_name: str, referrer_id: int | None):
    db = await _connect()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name, referrer_id) VALUES (?, ?, ?, ?)",
            (user_id, username, full_name, referrer_id),
        )
        await db.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
            (username, full_name, user_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_user(user_id: int):
    db = await _connect()
    try:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()
    finally:
        await db.close()


async def credit_referral_if_needed(user_id: int):
    """وقتی کاربر عضو چنل شد، معرف او را شارژ می‌کند."""
    db = await _connect()
    try:
        await db.execute(
            "UPDATE users SET referral_credited = 1 WHERE user_id = ? AND referrer_id IS NOT NULL",
            (user_id,),
        )
        await db.commit()
    finally:
        await db.close()


async def referral_count(user_id: int) -> int:
    db = await _connect()
    try:
        cur = await db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE referrer_id = ? AND referral_credited = 1",
            (user_id,),
        )
        row = await cur.fetchone()
        return row["c"]
    finally:
        await db.close()


async def top_referrers(limit: int = 10) -> list:
    db = await _connect()
    try:
        cur = await db.execute(
            """SELECT u.full_name, u.username, COUNT(r.user_id) AS c
               FROM users r JOIN users u ON u.user_id = r.referrer_id
               WHERE r.referral_credited = 1
               GROUP BY r.referrer_id
               ORDER BY c DESC
               LIMIT ?""",
            (limit,),
        )
        return await cur.fetchall()
    finally:
        await db.close()


async def get_setting(key: str, default: str | None = None) -> str | None:
    db = await _connect()
    try:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default
    finally:
        await db.close()


async def set_setting(key: str, value: str):
    db = await _connect()
    try:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()
    finally:
        await db.close()


async def _setting_int(key: str, default: int) -> int:
    value = await get_setting(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def required_referrals() -> int:
    return await _setting_int("required_referrals", DEFAULT_REQUIRED_REFERRALS)


async def daily_chat_limit() -> int:
    return await _setting_int("daily_chat_limit", DEFAULT_DAILY_CHAT_LIMIT)


async def daily_image_limit() -> int:
    return await _setting_int("daily_image_limit", DEFAULT_DAILY_IMAGE_LIMIT)


async def maintenance_on() -> bool:
    return await get_setting("maintenance", "0") == "1"


async def set_ban(user_id: int, banned: bool):
    db = await _connect()
    try:
        await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (int(banned), user_id))
        await db.commit()
    finally:
        await db.close()


async def mark_unlock_announced(user_id: int):
    db = await _connect()
    try:
        await db.execute("UPDATE users SET unlock_announced = 1 WHERE user_id = ?", (user_id,))
        await db.commit()
    finally:
        await db.close()


async def all_user_ids() -> list[int]:
    db = await _connect()
    try:
        cur = await db.execute("SELECT user_id FROM users WHERE is_banned = 0")
        return [row["user_id"] for row in await cur.fetchall()]
    finally:
        await db.close()


async def increment_usage(user_id: int, kind: str):
    column = "chat_count" if kind == "chat" else "image_count"
    db = await _connect()
    try:
        await db.execute(
            f"""INSERT INTO usage (user_id, day, {column}) VALUES (?, date('now'), 1)
                ON CONFLICT(user_id, day) DO UPDATE SET {column} = {column} + 1""",
            (user_id,),
        )
        await db.commit()
    finally:
        await db.close()


async def usage_today(user_id: int) -> dict:
    db = await _connect()
    try:
        cur = await db.execute(
            "SELECT chat_count, image_count FROM usage WHERE user_id = ? AND day = date('now')",
            (user_id,),
        )
        row = await cur.fetchone()
        return {"chat": row["chat_count"], "image": row["image_count"]} if row else {"chat": 0, "image": 0}
    finally:
        await db.close()


async def usage_totals() -> dict:
    db = await _connect()
    try:
        cur = await db.execute(
            """SELECT
                   COALESCE(SUM(chat_count), 0) AS chat_total,
                   COALESCE(SUM(image_count), 0) AS image_total,
                   COALESCE(SUM(CASE WHEN day = date('now') THEN chat_count END), 0) AS chat_today,
                   COALESCE(SUM(CASE WHEN day = date('now') THEN image_count END), 0) AS image_today
               FROM usage"""
        )
        return dict(await cur.fetchone())
    finally:
        await db.close()


async def stats() -> dict:
    db = await _connect()
    try:
        async def one(query, params=()):
            cur = await db.execute(query, params)
            return (await cur.fetchone())["c"]

        required = await required_referrals()
        usage = await usage_totals()
        return {
            "total": await one("SELECT COUNT(*) AS c FROM users"),
            "today": await one("SELECT COUNT(*) AS c FROM users WHERE date(created_at) = date('now')"),
            "banned": await one("SELECT COUNT(*) AS c FROM users WHERE is_banned = 1"),
            "unlocked": await one(
                """SELECT COUNT(*) AS c FROM (
                       SELECT referrer_id FROM users
                       WHERE referral_credited = 1
                       GROUP BY referrer_id
                       HAVING COUNT(*) >= ?
                   )""",
                (required,),
            ),
            "required": required,
            **usage,
        }
    finally:
        await db.close()
