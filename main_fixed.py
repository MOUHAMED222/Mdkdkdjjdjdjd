import sys
import subprocess
import importlib

REQUIRED_PACKAGES = {
    "aiogram": "aiogram==3.22.0",
    "telethon": "Telethon==1.45.0",
    "aiohttp": "aiohttp==3.12.15",
}

def install_missing_packages():
    for module, package in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(module)
        except ImportError:
            print(f"[+] Installing {package}...")
            subprocess.check_call([
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                package
            ])

install_missing_packages()
import asyncio
import json
import logging
import os
import random
import re
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    RPCError,
    SessionPasswordNeededError,
)
from telethon.tl.functions.photos import UploadProfilePhotoRequest


# ============================================================
# CONFIG
# ============================================================
# التثبيت:
#   pip install -r requirements.txt
#
# ملاحظة:
# تم إبقاء إعدادات البوت/API الحالية كما هي في الملف الأصلي.
# يفضّل لاحقًا نقلها إلى متغيرات بيئية وعدم مشاركة الملف.

BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "8988065622:AAEvDrt2eITreBUY7btwpxVypQbu1Mp6jHk",
)

API_ID = int(
    os.getenv("API_ID", "38071059")
)

API_HASH = os.getenv(
    "API_HASH",
    "114ad52cfe74e3c236dcab51eefd9963",
)

# لم يعد الهاتف يُؤخذ من الإعدادات.
# التسجيل يتم بالكامل من داخل البوت.
PHONE_NUMBER = ""

PIXABAY_API_KEY = os.getenv(
    "PIXABAY_API_KEY",
    "57863928-956730016635a9f8a565933cf",
).strip()

IMAGE_SOURCE = os.getenv(
    "IMAGE_SOURCE",
    "pixabay",
).strip().lower()

# ملف جلسة حساب Telegram الشخصي.
SESSION_NAME = os.getenv(
    "SESSION_NAME",
    "my_personal_account",
)

# ملف صغير لحفظ Telegram ID الخاص بمستخدم البوت المربوط بالحساب.
OWNER_FILE = Path(
    os.getenv(
        "OWNER_FILE",
        "bot_owner.json",
    )
)

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_AUTO_IMAGES = 20

# زيادة هذا إلى 3-4 تجعل التنزيل أسرع، بدون تخزين الصور كلها في الذاكرة.
DOWNLOAD_CONCURRENCY = 2

# مهلة صغيرة بين الرفعيات لتقليل احتمالات FloodWait.
# الرفع نفسه متسلسل لأن تحديث صورة الملف الشخصي عملية واحدة في كل لحظة.
DELAY_BETWEEN_UPLOADS = float(
    os.getenv(
        "DELAY_BETWEEN_UPLOADS",
        "0.5",
    )
)

DOWNLOAD_DIR = Path(
    os.getenv(
        "DOWNLOAD_DIR",
        "./profile_uploads",
    )
)
DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

DEFAULT_QUERIES = [
    "aesthetic nature",
    "dark aesthetic",
    "minimal aesthetic",
    "cinematic landscape",
    "mountains sunset",
    "ocean aesthetic",
    "city night aesthetic",
    "black and white portrait",
    "luxury architecture",
    "beautiful landscape",
]

QUERY_BUTTONS = {
    "🌿 طبيعة": "aesthetic nature",
    "🌑 داكن": "dark aesthetic",
    "✨ جمالي": "minimal aesthetic",
    "🎬 سينمائي": "cinematic landscape",
    "🏙 مدينة": "city night aesthetic",
    "🎲 عشوائي": None,
}


# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("profile-photo-bot")


# ============================================================
# BOT / TELETHON
# ============================================================
bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

user_client = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH,
)

upload_lock = asyncio.Lock()
login_lock = asyncio.Lock()
auto_lock = asyncio.Lock()
download_semaphore = asyncio.Semaphore(
    DOWNLOAD_CONCURRENCY
)

# Telegram ID للمطور صاحب الصلاحية الكاملة.
DEVELOPER_ID = 6891530912

# Telegram ID للمالك المرتبط بالحساب الشخصي.
OWNER_ID: Optional[int] = None

# طلبات تسجيل الدخول المؤقتة.
# لا نسجل الأكواد أو كلمات المرور في الـ log.
pending_login: dict[int, dict[str, str]] = {}

# حدث إلغاء عملية التحميل التلقائي.
auto_cancel_event = asyncio.Event()

http_session: Optional[aiohttp.ClientSession] = None


# ============================================================
# FSM STATES
# ============================================================
class LinkAccount(StatesGroup):
    phone = State()
    code = State()
    password = State()


class AutoSetup(StatesGroup):
    count = State()
    query = State()


# ============================================================
# KEYBOARDS
# ============================================================
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔐 ربط الحساب"),
                KeyboardButton(text="📷 إضافة صورة"),
            ],
            [
                KeyboardButton(text="🤖 صور تلقائية"),
                KeyboardButton(text="📊 الحالة"),
            ],
            [
                KeyboardButton(text="🛑 إلغاء"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="اختر عملية",
    )


def auto_count_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="5"),
                KeyboardButton(text="10"),
            ],
            [
                KeyboardButton(text="15"),
                KeyboardButton(text="20"),
            ],
            [
                KeyboardButton(text="🛑 إلغاء"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def query_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🌿 طبيعة"),
                KeyboardButton(text="🌑 داكن"),
            ],
            [
                KeyboardButton(text="✨ جمالي"),
                KeyboardButton(text="🎬 سينمائي"),
            ],
            [
                KeyboardButton(text="🏙 مدينة"),
                KeyboardButton(text="🎲 عشوائي"),
            ],
            [
                KeyboardButton(text="🛑 إلغاء"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="أو اكتب موضوع البحث",
    )


def login_cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛑 إلغاء")]
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ============================================================
# OWNER / STATE HELPERS
# ============================================================
def load_owner_id() -> int | None:
    try:
        if not OWNER_FILE.exists():
            return None

        data = json.loads(
            OWNER_FILE.read_text(
                encoding="utf-8"
            )
        )

        owner = data.get("owner_id")
        return int(owner) if owner is not None else None

    except Exception as exc:
        logger.warning(
            "تعذر قراءة ملف المالك: %s",
            exc,
        )
        return None


def save_owner_id(owner_id: int) -> None:
    OWNER_FILE.write_text(
        json.dumps(
            {"owner_id": owner_id},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def is_owner(message: Message) -> bool:
    return bool(
        message.from_user
        and message.chat.type == "private"
        and (
            message.from_user.id == DEVELOPER_ID
            or (
                OWNER_ID is not None
                and message.from_user.id == OWNER_ID
            )
        )
    )


async def reject_non_owner(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer(
            "هذا البوت مخصص للمالك المرتبط فقط."
        )


def clear_pending_login(user_id: int) -> None:
    pending_login.pop(user_id, None)


# ============================================================
# CONFIG CHECK
# ============================================================
def check_config() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN غير مضبوط."
        )

    if not API_ID:
        raise RuntimeError(
            "API_ID غير مضبوط."
        )

    if not API_HASH:
        raise RuntimeError(
            "API_HASH غير مضبوط."
        )

    if IMAGE_SOURCE not in {
        "pixabay",
        "wikimedia",
    }:
        raise RuntimeError(
            "IMAGE_SOURCE يجب أن تكون pixabay أو wikimedia."
        )

    if (
        IMAGE_SOURCE == "pixabay"
        and not PIXABAY_API_KEY
    ):
        logger.warning(
            "PIXABAY_API_KEY غير موجود، "
            "سيتم استخدام Wikimedia Commons."
        )


# ============================================================
# HTTP
# ============================================================
async def get_http_session() -> aiohttp.ClientSession:
    global http_session

    if (
        http_session is None
        or http_session.closed
    ):
        timeout = aiohttp.ClientTimeout(
            total=90,
            connect=10,
            sock_connect=10,
            sock_read=60,
        )

        connector = aiohttp.TCPConnector(
            limit=DOWNLOAD_CONCURRENCY + 2,
            limit_per_host=DOWNLOAD_CONCURRENCY,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )

        http_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "TelegramProfilePhotoBot/2.0"
                )
            },
        )

    return http_session


async def close_http_session() -> None:
    global http_session

    if http_session is not None:
        await http_session.close()
        http_session = None


async def async_get_json(
    url: str,
    headers: dict | None = None,
) -> dict:
    session = await get_http_session()

    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "TelegramProfilePhotoBot/2.0"
        )
    }

    if headers:
        request_headers.update(headers)

    async with session.get(
        url,
        headers=request_headers,
    ) as response:
        if response.status != 200:
            raise RuntimeError(
                f"HTTP {response.status}"
            )

        return await response.json(
            content_type=None
        )


async def async_download(
    url: str,
    destination: Path,
) -> None:
    """
    تنزيل Stream إلى القرص بدل تحميل الصورة كاملة
    إلى RAM. هذا يقلل استهلاك الذاكرة.
    """
    session = await get_http_session()

    async with download_semaphore:
        async with session.get(url) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"فشل تنزيل الصورة: HTTP {response.status}"
                )

            content_type = (
                response.headers.get(
                    "Content-Type",
                    "",
                )
                or ""
            ).lower()

            if (
                content_type
                and not content_type.startswith(
                    "image/"
                )
                and "octet-stream" not in content_type
            ):
                raise ValueError(
                    "المصدر لم يُرجع صورة."
                )

            total = 0

            with destination.open(
                "wb"
            ) as file_handle:
                async for chunk in response.content.iter_chunked(
                    64 * 1024
                ):
                    total += len(chunk)

                    if total > MAX_FILE_SIZE:
                        raise ValueError(
                            "الصورة أكبر من 20MB."
                        )

                    file_handle.write(chunk)

            if total == 0:
                raise ValueError(
                    "الصورة فارغة."
                )


# ============================================================
# IMAGE SEARCH
# ============================================================
def clean_query(query: str) -> str:
    query = re.sub(
        r"\s+",
        " ",
        query,
    ).strip()

    return query[:100]


async def search_pixabay(
    query: str,
    count: int,
) -> list[dict]:
    if not PIXABAY_API_KEY:
        return []

    params = {
        "key": PIXABAY_API_KEY,
        "q": clean_query(query),
        "image_type": "photo",
        "orientation": "all",
        "safesearch": "true",
        "per_page": min(
            max(count * 3, 20),
            100,
        ),
        "page": 1,
    }

    url = (
        "https://pixabay.com/api/?"
        + urlencode(params)
    )

    data = await async_get_json(url)

    results = []

    for item in data.get(
        "hits",
        [],
    ):
        image_url = (
            item.get("largeImageURL")
            or item.get("webformatURL")
            or item.get("previewURL")
        )

        if not image_url:
            continue

        results.append(
            {
                "url": image_url,
                "source": "Pixabay",
                "page_url": item.get(
                    "pageURL",
                    "",
                ),
                "id": str(
                    item.get(
                        "id",
                        "",
                    )
                ),
                "tags": item.get(
                    "tags",
                    "",
                ),
            }
        )

        if len(results) >= count:
            break

    return results


async def search_wikimedia(
    query: str,
    count: int,
) -> list[dict]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": clean_query(query),
        "gsrnamespace": "6",
        "gsrlimit": min(
            max(count * 4, 20),
            50,
        ),
        "prop": "imageinfo",
        "iiprop": "url|mime",
        "iiurlwidth": 1280,
    }

    url = (
        "https://commons.wikimedia.org/w/api.php?"
        + urlencode(params)
    )

    data = await async_get_json(
        url,
        headers={
            "User-Agent": (
                "TelegramProfilePhotoBot/2.0 "
                "(image search bot)"
            )
        },
    )

    pages = (
        data.get(
            "query",
            {},
        ).get(
            "pages",
            {},
        )
    )

    results = []

    for page in pages.values():
        title = page.get(
            "title",
            "",
        )

        image_info = (
            page.get(
                "imageinfo"
            )
            or []
        )

        if not image_info:
            continue

        info = image_info[0]
        mime = (
            info.get(
                "mime",
                "",
            )
            or ""
        ).lower()

        if not mime.startswith(
            "image/"
        ):
            continue

        image_url = (
            info.get("thumburl")
            or info.get("url")
        )

        if not image_url:
            continue

        results.append(
            {
                "url": image_url,
                "source": "Wikimedia Commons",
                "page_url": (
                    "https://commons.wikimedia.org/wiki/"
                    + title.replace(
                        " ",
                        "_",
                    )
                ),
                "id": str(
                    page.get(
                        "pageid",
                        "",
                    )
                ),
                "tags": title,
            }
        )

        if len(results) >= count:
            break

    return results


async def search_images(
    query: str,
    count: int,
) -> list[dict]:
    results: list[dict] = []

    # يحاول المصدر المحدد أولًا.
    if (
        IMAGE_SOURCE == "pixabay"
        and PIXABAY_API_KEY
    ):
        try:
            results = await search_pixabay(
                query,
                count,
            )
        except Exception as exc:
            logger.warning(
                "فشل بحث Pixabay: %s",
                exc,
            )

    # fallback تلقائي.
    if not results:
        try:
            results = await search_wikimedia(
                query,
                count,
            )
        except Exception as exc:
            logger.warning(
                "فشل بحث Wikimedia: %s",
                exc,
            )

    unique = []
    seen = set()

    for item in results:
        image_url = item.get(
            "url",
            "",
        )

        if not image_url:
            continue

        if image_url in seen:
            continue

        seen.add(image_url)
        unique.append(item)

    return unique[:count]


# ============================================================
# FILE HELPERS
# ============================================================
def temp_image_path(
    prefix: str,
    suffix: str = ".jpg",
) -> Path:
    fd, temp_name = tempfile.mkstemp(
        prefix=prefix,
        suffix=suffix,
        dir=DOWNLOAD_DIR,
    )
    os.close(fd)
    return Path(temp_name)


def guess_suffix(url: str) -> str:
    lower = url.lower()

    if ".png" in lower:
        return ".png"

    if ".webp" in lower:
        return ".webp"

    if ".jpeg" in lower:
        return ".jpeg"

    return ".jpg"


# ============================================================
# TELEGRAM PROFILE PHOTO UPLOAD
# ============================================================
async def upload_as_profile_photo(
    path: Path,
) -> None:
    async with upload_lock:
        for attempt in range(2):
            try:
                uploaded_file = (
                    await user_client.upload_file(
                        str(path)
                    )
                )

                await user_client(
                    UploadProfilePhotoRequest(
                        file=uploaded_file,
                    )
                )

                if DELAY_BETWEEN_UPLOADS > 0:
                    await asyncio.sleep(
                        DELAY_BETWEEN_UPLOADS
                    )

                return

            except FloodWaitError as exc:
                if attempt == 1:
                    raise

                wait_seconds = max(
                    int(exc.seconds),
                    1,
                )

                logger.warning(
                    "FloodWait: %s ثانية.",
                    wait_seconds,
                )

                await asyncio.sleep(
                    wait_seconds + 1
                )

            except RPCError:
                raise


# ============================================================
# DOWNLOAD / UPLOAD PIPELINE
# ============================================================
async def download_one(
    item: dict,
    index: int,
) -> tuple[Path, dict, int]:
    path = temp_image_path(
        prefix=f"auto_{index}_",
        suffix=guess_suffix(
            item.get(
                "url",
                "",
            )
        ),
    )

    try:
        await async_download(
            item["url"],
            path,
        )
        return path, item, index

    except Exception:
        path.unlink(
            missing_ok=True
        )
        raise


async def process_auto_images(
    message: Message,
    count: int,
    query: str,
) -> None:
    global auto_cancel_event

    async with auto_lock:
        auto_cancel_event = asyncio.Event()

        status = await message.answer(
            "🔎 جاري البحث عن الصور...",
            reply_markup=main_menu(),
        )

        try:
            images = await search_images(
                query,
                count,
            )

            if not images:
                await status.edit_text(
                    "❌ لم أجد صورًا مناسبة لهذا البحث."
                )
                return

            total = len(images)

            await status.edit_text(
                f"📸 تم العثور على {total} صور.\n"
                "⬇️ تجهيز التحميل والرفع..."
            )

            success = 0
            failed = 0
            used_sources: dict[str, int] = {}

            pending: set[
                asyncio.Task
            ] = set()

            next_index = 0

            # Pipeline محدود: لا نحمل الـ20 صورة دفعة واحدة.
            while (
                next_index < total
                and len(pending)
                < DOWNLOAD_CONCURRENCY
            ):
                task = asyncio.create_task(
                    download_one(
                        images[next_index],
                        next_index + 1,
                    )
                )

                pending.add(task)
                next_index += 1

            while pending:
                if auto_cancel_event.is_set():
                    for task in pending:
                        task.cancel()

                    await asyncio.gather(
                        *pending,
                        return_exceptions=True,
                    )
                    break

                done, pending = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in done:
                    if auto_cancel_event.is_set():
                        break

                    path = None
                    try:
                        path, item, index = (
                            await task
                        )

                        await status.edit_text(
                            f"⬆️ إضافة الصورة "
                            f"{index}/{total}..."
                        )

                        await upload_as_profile_photo(
                            path
                        )

                        success += 1

                        source = item.get(
                            "source",
                            "Unknown",
                        )

                        used_sources[source] = (
                            used_sources.get(
                                source,
                                0,
                            )
                            + 1
                        )

                    except (
                        FloodWaitError
                    ) as exc:
                        failed += 1

                        await status.edit_text(
                            "⏳ Telegram طلب الانتظار "
                            f"{exc.seconds} ثانية.\n"
                            f"تمت إضافة {success} صورة، "
                            "وتوقفت العملية."
                        )

                        auto_cancel_event.set()

                        for pending_task in pending:
                            pending_task.cancel()

                        await asyncio.gather(
                            *pending,
                            return_exceptions=True,
                        )

                        return

                    except Exception as exc:
                        failed += 1

                        logger.warning(
                            "فشل في صورة تلقائية: %s",
                            exc,
                        )

                    finally:
                        if path:
                            path.unlink(
                                missing_ok=True
                            )

                    if (
                        next_index < total
                        and not auto_cancel_event.is_set()
                    ):
                        new_task = asyncio.create_task(
                            download_one(
                                images[next_index],
                                next_index + 1,
                            )
                        )

                        pending.add(
                            new_task
                        )

                        next_index += 1

            if auto_cancel_event.is_set():
                await status.edit_text(
                    "🛑 تم إيقاف العملية.\n\n"
                    f"تمت الإضافة: {success}\n"
                    f"فشل/تخطي: {failed}",
                )

                return

            sources_text = ", ".join(
                f"{name}: {num}"
                for name, num in used_sources.items()
            ) or "غير معروف"

            await status.edit_text(
                "✅ اكتملت العملية.\n\n"
                f"تمت الإضافة: {success}\n"
                f"فشل/تخطي: {failed}\n"
                f"المصدر: {sources_text}",
            )

        except Exception as exc:
            logger.exception(
                "Auto process failed: %s",
                exc,
            )

            await status.edit_text(
                "❌ حدث خطأ أثناء البحث أو التحميل.\n"
                "راجع سجل التشغيل.",
            )


# ============================================================
# START / CANCEL
# ============================================================
@dp.message(CommandStart())
async def start_handler(
    message: Message,
    state: FSMContext,
) -> None:
    await state.clear()

    if (
        OWNER_ID is not None
        and not is_owner(message)
    ):
        return await reject_non_owner(
            message
        )

    if OWNER_ID is None:
        await message.answer(
            "👋 أهلًا بك.\n\n"
            "ابدأ بربط حساب Telegram من الزر "
            "🔐 ربط الحساب.\n\n"
            "بعد نجاح الربط تصبح أنت المالك الوحيد "
            "للبوت.",
            reply_markup=main_menu(),
        )
        return

    await message.answer(
        "✅ البوت جاهز.\n\n"
        "📷 إضافة صورة: أرسل صورة وسأضعها "
        "كصورة ملف شخصي.\n\n"
        "🤖 صور تلقائية: اختر العدد والموضوع.",
        reply_markup=main_menu(),
    )


@dp.message(
    Command("cancel")
)
@dp.message(
    F.text == "🛑 إلغاء"
)
async def cancel_handler(
    message: Message,
    state: FSMContext,
) -> None:
    current_state = await state.get_state()
    await state.clear()

    if (
        auto_lock.locked()
        and is_owner(message)
    ):
        auto_cancel_event.set()

    if message.from_user:
        clear_pending_login(
            message.from_user.id
        )

    # لا نغلق الحساب الشخصي، فقط نلغي الخطوة الحالية.
    await message.answer(
        "🛑 تم الإلغاء.",
        reply_markup=main_menu(),
    )


# ============================================================
# LINK ACCOUNT
# ============================================================
@dp.message(
    F.text == "🔐 ربط الحساب"
)
async def link_account_start(
    message: Message,
    state: FSMContext,
) -> None:
    global OWNER_ID

    if not message.from_user:
        return

    if OWNER_ID is not None:
        if not is_owner(message):
            return await reject_non_owner(
                message
            )

        if user_client.is_connected():
            try:
                if await user_client.is_user_authorized():
                    me = await user_client.get_me()

                    username = (
                        f"@{me.username}"
                        if me.username
                        else "بدون username"
                    )

                    await message.answer(
                        "✅ الحساب مرتبط بالفعل.\n\n"
                        f"👤 {username}\n"
                        f"🆔 {me.id}",
                        reply_markup=main_menu(),
                    )
                    return

            except Exception:
                pass

        await message.answer(
            "⚠️ الحساب المربوط غير متصل حاليًا.\n"
            "أعد تشغيل البوت أو تحقّق من ملف الجلسة.",
            reply_markup=main_menu(),
        )
        return

    if message.chat.type != "private":
        return

    async with login_lock:
        try:
            if not user_client.is_connected():
                await user_client.connect()

            if await user_client.is_user_authorized():
                me = await user_client.get_me()

                OWNER_ID = message.from_user.id
                save_owner_id(
                    OWNER_ID
                )

                await state.clear()

                await message.answer(
                    "✅ تم ربط جلسة الحساب الموجودة "
                    "بالبوت بنجاح.",
                    reply_markup=main_menu(),
                )
                logger.info(
                    "تم ربط حسابًا موجودًا مسبقًا."
                )
                return

            await state.set_state(
                LinkAccount.phone
            )

            await message.answer(
                "📱 أرسل رقم الهاتف المرتبط بحساب Telegram.\n\n"
                "مثال:\n"
                "+213xxxxxxxxx",
                reply_markup=login_cancel_menu(),
            )

        except Exception as exc:
            logger.exception(
                "فشل بدء تسجيل الدخول: %s",
                exc,
            )

            await message.answer(
                "❌ تعذر بدء تسجيل الدخول.",
                reply_markup=main_menu(),
            )


@dp.message(
    LinkAccount.phone,
    F.text,
)
async def receive_phone(
    message: Message,
    state: FSMContext,
) -> None:
    phone = (
        message.text
        or ""
    ).strip()

    if phone == "🛑 إلغاء":
        return await cancel_handler(
            message,
            state,
        )

    if not re.fullmatch(
        r"\+?\d[\d\s\-]{7,20}",
        phone,
    ):
        await message.answer(
            "❌ صيغة الرقم غير صحيحة.\n"
            "أرسله بصيغة دولية مثل +213xxxxxxxxx.",
            reply_markup=login_cancel_menu(),
        )
        return

    phone = re.sub(
        r"[\s\-]",
        "",
        phone,
    )

    async with login_lock:
        try:
            if not user_client.is_connected():
                await user_client.connect()

            sent = await user_client.send_code_request(
                phone
            )

            pending_login[
                message.from_user.id
            ] = {
                "phone": phone,
                "phone_code_hash": sent.phone_code_hash,
            }

            await state.set_state(
                LinkAccount.code
            )

            await message.answer(
                "🔢 تم إرسال كود Telegram.\n"
                "أرسل الكود هنا كما وصلك.\n\n"
                "⚠️ لا ترسل الكود لأي شخص آخر.",
                reply_markup=login_cancel_menu(),
            )

        except RPCError as exc:
            logger.warning(
                "فشل إرسال كود الدخول: %s",
                exc,
            )

            await message.answer(
                "❌ لم أستطع إرسال كود الدخول.\n"
                "تحقق من الرقم وحاول مرة أخرى.",
                reply_markup=login_cancel_menu(),
            )


@dp.message(
    LinkAccount.code,
    F.text,
)
async def receive_code(
    message: Message,
    state: FSMContext,
) -> None:
    if not message.from_user:
        return

    code = re.sub(
        r"\s+",
        "",
        message.text or "",
    )

    if (
        not code.isdigit()
        or not 4 <= len(code) <= 8
    ):
        await message.answer(
            "❌ أرسل كود Telegram فقط.",
            reply_markup=login_cancel_menu(),
        )
        return

    login_data = pending_login.get(
        message.from_user.id
    )

    if not login_data:
        await state.clear()
        await message.answer(
            "⚠️ انتهت جلسة تسجيل الدخول.\n"
            "ابدأ من زر 🔐 ربط الحساب.",
            reply_markup=main_menu(),
        )
        return

    async with login_lock:
        try:
            result = await user_client.sign_in(
                phone=login_data["phone"],
                code=code,
                phone_code_hash=(
                    login_data["phone_code_hash"]
                ),
            )

            await finish_login(
                message,
                state,
                result,
            )

        except SessionPasswordNeededError:
            await state.set_state(
                LinkAccount.password
            )

            await message.answer(
                "🔐 الحساب عليه التحقق بخطوتين.\n"
                "أرسل كلمة مرور التحقق بخطوتين.",
                reply_markup=login_cancel_menu(),
            )

        except PhoneCodeInvalidError:
            await message.answer(
                "❌ الكود غير صحيح.\n"
                "أرسل الكود مرة أخرى.",
                reply_markup=login_cancel_menu(),
            )

        except PhoneCodeExpiredError:
            clear_pending_login(
                message.from_user.id
            )
            await state.set_state(
                LinkAccount.phone
            )

            await message.answer(
                "⌛ انتهت صلاحية الكود.\n"
                "أرسل رقم الهاتف لإرسال كود جديد.",
                reply_markup=login_cancel_menu(),
            )

        except RPCError as exc:
            logger.warning(
                "فشل تسجيل الدخول بالكود: %s",
                exc,
            )

            await message.answer(
                "❌ فشل تسجيل الدخول.\n"
                "جرّب إرسال الكود مرة أخرى أو ألغِ العملية.",
                reply_markup=login_cancel_menu(),
            )


@dp.message(
    LinkAccount.password,
    F.text,
)
async def receive_password(
    message: Message,
    state: FSMContext,
) -> None:
    if not message.from_user:
        return

    password = (
        message.text
        or ""
    ).strip()

    if password == "🛑 إلغاء":
        return await cancel_handler(
            message,
            state,
        )

    login_data = pending_login.get(
        message.from_user.id
    )

    if not login_data:
        await state.clear()
        await message.answer(
            "⚠️ انتهت جلسة تسجيل الدخول.",
            reply_markup=main_menu(),
        )
        return

    async with login_lock:
        try:
            result = await user_client.sign_in(
                password=password
            )

            await finish_login(
                message,
                state,
                result,
            )

        except PasswordHashInvalidError:
            await message.answer(
                "❌ كلمة مرور التحقق بخطوتين غير صحيحة.",
                reply_markup=login_cancel_menu(),
            )

        except RPCError as exc:
            logger.warning(
                "فشل تسجيل الدخول بكلمة المرور: %s",
                exc,
            )

            await message.answer(
                "❌ تعذر إكمال تسجيل الدخول.",
                reply_markup=login_cancel_menu(),
            )


async def finish_login(
    message: Message,
    state: FSMContext,
    result,
) -> None:
    global OWNER_ID

    me = result

    if me is None:
        me = await user_client.get_me()

    OWNER_ID = message.from_user.id
    save_owner_id(
        OWNER_ID
    )

    clear_pending_login(
        message.from_user.id
    )

    await state.clear()

    username = (
        f"@{me.username}"
        if me.username
        else "بدون username"
    )

    await message.answer(
        "✅ تم ربط الحساب بنجاح.\n\n"
        f"👤 {username}\n"
        f"🆔 Telegram ID: {me.id}\n\n"
        "من الآن تستطيع استخدام لوحة الأزرار "
        "لإضافة الصور إلى ملفك الشخصي.",
        reply_markup=main_menu(),
    )

    logger.info(
        "تم تسجيل الدخول وربط الحساب بنجاح."
    )


# ============================================================
# STATUS
# ============================================================
@dp.message(
    F.text == "📊 الحالة"
)
async def status_button(
    message: Message,
) -> None:
    if not is_owner(message):
        return await reject_non_owner(
            message
        )

    connected = user_client.is_connected()

    try:
        if not connected:
            await user_client.connect()

        authorized = (
            await user_client.is_user_authorized()
        )

        if not authorized:
            await message.answer(
                "🔴 الحساب الشخصي غير مسجل الدخول.",
                reply_markup=main_menu(),
            )
            return

        me = await user_client.get_me()

        username = (
            f"@{me.username}"
            if me.username
            else "بدون username"
        )

        source = (
            "Pixabay → Wikimedia"
            if PIXABAY_API_KEY
            else "Wikimedia Commons"
        )

        await message.answer(
            "🟢 البوت يعمل\n"
            f"🟢 الحساب: متصل\n"
            f"👤 {username}\n"
            f"🆔 {me.id}\n"
            f"🖼 المصدر: {source}",
            reply_markup=main_menu(),
        )

    except Exception as exc:
        logger.warning(
            "فشل فحص الحالة: %s",
            exc,
        )

        await message.answer(
            "⚠️ تعذر الحصول على حالة الحساب.",
            reply_markup=main_menu(),
        )


@dp.message(
    Command("status")
)
async def status_command(
    message: Message,
) -> None:
    await status_button(message)


@dp.message(
    Command("id")
)
async def id_command(
    message: Message,
) -> None:
    if not message.from_user:
        return

    await message.answer(
        f"Telegram ID: {message.from_user.id}",
        reply_markup=main_menu(),
    )


# ============================================================
# MANUAL IMAGE
# ============================================================
@dp.message(
    F.text == "📷 إضافة صورة"
)
async def manual_mode(
    message: Message,
    state: FSMContext,
) -> None:
    if not is_owner(message):
        return await reject_non_owner(
            message
        )

    await state.clear()

    await message.answer(
        "📷 أرسل الصورة الآن، وسأضعها في صور ملفك الشخصي.",
        reply_markup=main_menu(),
    )


async def download_telegram_file(
    message: Message,
) -> Path:
    file_id = None
    original_name = "profile.jpg"

    if message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        original_name = (
            f"profile_{photo.file_unique_id}.jpg"
        )

        if (
            photo.file_size
            and photo.file_size > MAX_FILE_SIZE
        ):
            raise ValueError(
                "حجم الصورة أكبر من 20MB."
            )

    elif message.document:
        document = message.document
        mime = (
            document.mime_type
            or ""
        ).lower()

        if not mime.startswith(
            "image/"
        ):
            raise ValueError(
                "أرسل صورة فقط."
            )

        file_id = document.file_id
        original_name = (
            document.file_name
            or f"profile_{document.file_unique_id}"
        )

        if (
            document.file_size
            and document.file_size > MAX_FILE_SIZE
        ):
            raise ValueError(
                "حجم الصورة أكبر من 20MB."
            )

    else:
        raise ValueError(
            "أرسل صورة فقط."
        )

    suffix = Path(
        original_name
    ).suffix.lower()

    if suffix not in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".bmp",
        ".gif",
    }:
        suffix = ".jpg"

    target = temp_image_path(
        prefix="telegram_",
        suffix=suffix,
    )

    try:
        tg_file = await bot.get_file(
            file_id
        )

        if not tg_file.file_path:
            raise RuntimeError(
                "تعذر الحصول على مسار الملف."
            )

        await bot.download_file(
            tg_file.file_path,
            destination=target,
        )

        if (
            target.stat().st_size
            > MAX_FILE_SIZE
        ):
            raise ValueError(
                "حجم الصورة أكبر من 20MB."
            )

        return target

    except Exception:
        target.unlink(
            missing_ok=True
        )
        raise


async def process_manual_image(
    message: Message,
) -> None:
    path: Path | None = None

    status = await message.answer(
        "⏳ يتم رفع الصورة...",
        reply_markup=main_menu(),
    )

    try:
        path = await download_telegram_file(
            message
        )

        await upload_as_profile_photo(
            path
        )

        await status.edit_text(
            "✅ تم إضافة الصورة إلى صور ملفك الشخصي."
        )

    except FloodWaitError as exc:
        await status.edit_text(
            "⏳ Telegram طلب الانتظار "
            f"{exc.seconds} ثانية."
        )

    except ValueError as exc:
        await status.edit_text(
            f"❌ {exc}"
        )

    except RPCError as exc:
        logger.warning(
            "Telegram RPC error: %s",
            exc,
        )

        await status.edit_text(
            "❌ Telegram رفض رفع الصورة."
        )

    except Exception as exc:
        logger.exception(
            "Manual upload error: %s",
            exc,
        )

        await status.edit_text(
            "❌ حدث خطأ أثناء رفع الصورة."
        )

    finally:
        if path:
            path.unlink(
                missing_ok=True
            )


# لا نعالج الصور أثناء حالات تسجيل الدخول أو الإعداد.
@dp.message(
    F.photo
)
async def manual_photo_handler(
    message: Message,
    state: FSMContext,
) -> None:
    if await state.get_state():
        return

    if not is_owner(message):
        return await reject_non_owner(
            message
        )

    await process_manual_image(
        message
    )


@dp.message(
    F.document
)
async def manual_document_handler(
    message: Message,
    state: FSMContext,
) -> None:
    if await state.get_state():
        return

    if not is_owner(message):
        return await reject_non_owner(
            message
        )

    document = message.document

    if not document:
        return

    if not (
        (
            document.mime_type
            or ""
        ).lower().startswith(
            "image/"
        )
    ):
        return

    await process_manual_image(
        message
    )


# ============================================================
# AUTO MODE
# ============================================================
@dp.message(
    F.text == "🤖 صور تلقائية"
)
async def auto_button(
    message: Message,
    state: FSMContext,
) -> None:
    if not is_owner(message):
        return await reject_non_owner(
            message
        )

    await state.set_state(
        AutoSetup.count
    )

    await message.answer(
        "🔢 اختر عدد الصور:",
        reply_markup=auto_count_menu(),
    )


def parse_count(text: str) -> int:
    try:
        value = int(
            text.strip()
        )
    except ValueError:
        raise ValueError(
            "أدخل عددًا من 1 إلى 20."
        )

    if not 1 <= value <= MAX_AUTO_IMAGES:
        raise ValueError(
            "العدد يجب أن يكون من 1 إلى 20."
        )

    return value


@dp.message(
    AutoSetup.count,
    F.text,
)
async def auto_count_handler(
    message: Message,
    state: FSMContext,
) -> None:
    if (
        message.text
        == "🛑 إلغاء"
    ):
        return await cancel_handler(
            message,
            state,
        )

    try:
        count = parse_count(
            message.text
        )
    except ValueError as exc:
        await message.answer(
            f"❌ {exc}",
            reply_markup=auto_count_menu(),
        )
        return

    await state.update_data(
        count=count
    )
    await state.set_state(
        AutoSetup.query
    )

    await message.answer(
        "🎨 اختر نوع الصور أو اكتب كلمة البحث:\n\n"
        "مثال: cars / nature / night / black aesthetic",
        reply_markup=query_menu(),
    )


@dp.message(
    AutoSetup.query,
    F.text,
)
async def auto_query_handler(
    message: Message,
    state: FSMContext,
) -> None:
    if (
        message.text
        == "🛑 إلغاء"
    ):
        return await cancel_handler(
            message,
            state,
        )

    data = await state.get_data()

    count = int(
        data.get(
            "count",
            0,
        )
    )

    if not count:
        await state.clear()
        await message.answer(
            "⚠️ أعد العملية من زر 🤖 صور تلقائية.",
            reply_markup=main_menu(),
        )
        return

    selected = QUERY_BUTTONS.get(
        message.text or "",
        "__custom__",
    )

    if selected is None:
        query = random.choice(
            DEFAULT_QUERIES
        )
    elif selected == "__custom__":
        query = clean_query(
            message.text or ""
        )
    else:
        query = selected

    if not query:
        query = random.choice(
            DEFAULT_QUERIES
        )

    await state.clear()

    await message.answer(
        f"🚀 بدء العملية:\n"
        f"العدد: {count}\n"
        f"البحث: {query}",
        reply_markup=main_menu(),
    )

    await process_auto_images(
        message,
        count,
        query,
    )


# أوامر نصية سابقة تبقى مدعومة.
def parse_auto_command(
    text: str,
) -> tuple[int, str]:
    parts = text.strip().split(
        maxsplit=2
    )

    if len(parts) < 2:
        raise ValueError(
            "الاستخدام:\n"
            "/auto 10\n"
            "أو\n"
            "/auto 10 nature"
        )

    count = parse_count(
        parts[1]
    )

    if len(parts) == 3:
        query = clean_query(
            parts[2]
        )
    else:
        query = random.choice(
            DEFAULT_QUERIES
        )

    if not query:
        query = random.choice(
            DEFAULT_QUERIES
        )

    return count, query


@dp.message(
    Command("auto")
)
async def auto_command(
    message: Message,
) -> None:
    if not is_owner(message):
        return await reject_non_owner(
            message
        )

    try:
        count, query = parse_auto_command(
            message.text or ""
        )
    except ValueError as exc:
        await message.answer(
            f"❌ {exc}",
            reply_markup=main_menu(),
        )
        return

    await message.answer(
        f"🚀 بدء: {count} صور - {query}",
        reply_markup=main_menu(),
    )

    await process_auto_images(
        message,
        count,
        query,
    )


# ============================================================
# FALLBACK
# ============================================================
@dp.message()
async def fallback_handler(
    message: Message,
    state: FSMContext,
) -> None:
    if await state.get_state():
        return

    if not is_owner(message):
        if OWNER_ID is None:
            await message.answer(
                "اضغط 🔐 ربط الحساب للبدء.",
                reply_markup=main_menu(),
            )
        else:
            await reject_non_owner(
                message
            )
        return

    if message.chat.type == "private":
        await message.answer(
            "اختر عملية من الأزرار.",
            reply_markup=main_menu(),
        )


# ============================================================
# MAIN
# ============================================================
async def main() -> None:
    global OWNER_ID

    check_config()

    OWNER_ID = load_owner_id()

    logger.info(
        "بدء البوت..."
    )

    # نفتح اتصال Telethon فقط عند الحاجة/الفحص.
    await user_client.connect()

    # إذا كانت الجلسة محفوظة ومصرّح بها، نستعيد الحساب.
    try:
        if await user_client.is_user_authorized():
            me = await user_client.get_me()

            if me:
                logger.info(
                    "جلسة Telegram محفوظة ومصرّح بها."
                )

                # إن كان ملف المالك مفقودًا،
                # لا نغيّر المالك تلقائيًا لأمان أفضل.
                if OWNER_ID is None:
                    logger.info(
                        "لا يوجد OWNER_ID محفوظ؛ "
                        "سيتم ربطه من داخل البوت."
                    )
    except Exception as exc:
        logger.warning(
            "تعذر فحص الجلسة المحفوظة: %s",
            exc,
        )

    bot_info = await bot.get_me()

    logger.info(
        "البوت: @%s | ID=%s",
        bot_info.username,
        bot_info.id,
    )

    try:
        await dp.start_polling(
            bot
        )
    finally:
        await close_http_session()

        if user_client.is_connected():
            await user_client.disconnect()

        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(
            main()
        )
    except KeyboardInterrupt:
        logger.info(
            "تم إيقاف البرنامج."
        )
