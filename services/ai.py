import random
from urllib.parse import quote

import aiohttp

from config import GEMINI_API_KEY, GEMINI_MODEL

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

SYSTEM_PROMPT = (
    "You are a helpful assistant inside a Telegram bot. "
    "Always answer in the same language the user writes in (usually Persian). "
    "Keep answers clear and not too long."
)

IMAGE_SIZES = {
    "1:1": (1024, 1024),
    "16:9": (1280, 720),
    "9:16": (720, 1280),
}

_histories: dict[int, list[dict]] = {}
MAX_HISTORY = 10  # آخرین پیام‌های نگهداری‌شده برای هر کاربر


async def gemini_chat(user_id: int, text: str) -> str:
    history = _histories.setdefault(user_id, [])
    history.append({"role": "user", "parts": [{"text": text}]})

    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": history[-MAX_HISTORY:],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(GEMINI_URL, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                data = await resp.json()
        reply = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        history.pop()
        return "⚠️ خطایی در ارتباط با هوش مصنوعی رخ داد. لطفاً کمی بعد دوباره تلاش کن."

    history.append({"role": "model", "parts": [{"text": reply}]})
    del history[:-MAX_HISTORY]
    return reply


def clear_history(user_id: int):
    _histories.pop(user_id, None)


def image_url(prompt: str, ratio: str = "1:1") -> str:
    width, height = IMAGE_SIZES.get(ratio, IMAGE_SIZES["1:1"])
    safe_prompt = quote(prompt[:500])
    seed = random.randint(1, 999_999)
    return (
        f"https://image.pollinations.ai/prompt/{safe_prompt}"
        f"?width={width}&height={height}&nologo=true&model=flux&seed={seed}"
    )
