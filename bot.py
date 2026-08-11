import asyncio
import html
import logging
import os
import re
from collections import defaultdict
from contextlib import suppress
from typing import Optional

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
DB_PATH = os.getenv("DB_PATH", "scanner.db")

DEFAULT_MIN_SCANS = 20
DEFAULT_MAX_SCANS = 100

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    raise RuntimeError(
        "BOT_TOKEN environment variable is missing. "
        "Set BOT_TOKEN in Railway Variables."
    )

try:
    ADMIN_IDS = [
        int(x.strip())
        for x in ADMIN_IDS_RAW.split(",")
        if x.strip()
    ]
except ValueError as exc:
    raise RuntimeError(
        "ADMIN_IDS must contain comma-separated numeric Telegram user IDs."
    ) from exc


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)

logger = logging.getLogger("WA-Scanner")


# ============================================================
# FSM STATES
# ============================================================

class AdminState(StatesGroup):
    waiting_add_number = State()
    waiting_remove_number = State()
    waiting_set_min = State()
    waiting_set_max = State()
    waiting_scan_link = State()


# ============================================================
# DATABASE
# ============================================================

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS wa_numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT UNIQUE NOT NULL,
                added_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        await db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
            ("min_scans", str(DEFAULT_MIN_SCANS)),
        )

        await db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
            ("max_scans", str(DEFAULT_MAX_SCANS)),
        )

        await db.commit()


async def db_get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ) as cur:
            row = await cur.fetchone()

    return row[0] if row else ""


async def db_set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
            (key, value),
        )
        await db.commit()


async def db_add_number(number: str) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO wa_numbers(number) VALUES(?)",
                (number,),
            )
            await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def db_remove_number(number: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM wa_numbers WHERE number = ?",
            (number,),
        )
        await db.commit()
        return cur.rowcount > 0


async def db_get_all_numbers() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT number FROM wa_numbers ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()

    return [row[0] for row in rows]


async def db_clear_numbers() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM wa_numbers")
        await db.commit()


# ============================================================
# ACTIVE SCANS
# ============================================================

active_scans: dict[int, asyncio.Task] = {}

user_results: dict[int, dict] = defaultdict(
    lambda: {
        "numbers": [],
        "hidden": False,
        "url": "",
        "scans_done": 0,
        "scans_total": 0,
        "stopped": False,
    }
)


# ============================================================
# WHATSAPP NUMBER EXTRACTION
# ============================================================

WA_PATTERNS = [
    r"wa\.me/(\d{7,15})",
    r"api\.whatsapp\.com/send\?phone=(\d{7,15})",
    r"whatsapp://send\?phone=(\d{7,15})",
    r"\+(\d{1,3}[\s\-]?\d{5,12})",
    r"\b(91\d{10}|\d{10})\b",
    r"tel:\+?(\d{7,15})",
]


def extract_wa_numbers(text: str) -> list[str]:
    if not text:
        return []

    found: set[str] = set()

    for pattern in WA_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            raw = re.sub(r"[\s\-]", "", match.group(1))
            digits = re.sub(r"\D", "", raw)

            if 7 <= len(digits) <= 15:
                found.add(digits)

    return sorted(found)


# ============================================================
# HTTP FETCH
# ============================================================

async def fetch_url_and_extract(
    session: aiohttp.ClientSession,
    url: str,
) -> tuple[list[str], Optional[str]]:
    """
    Fetch a URL with redirects enabled and extract WhatsApp numbers
    from the final URL and response body.

    Use this only with links you own or are authorized to test.
    """

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }

    numbers: set[str] = set()
    final_url: Optional[str] = None

    try:
        timeout = aiohttp.ClientTimeout(
            total=15,
            connect=8,
            sock_read=10,
        )

        async with session.get(
            url,
            headers=headers,
            allow_redirects=True,
            timeout=timeout,
            ssl=False,
        ) as response:
            final_url = str(response.url)

            numbers.update(extract_wa_numbers(final_url))

            content_type = response.headers.get("Content-Type", "").lower()

            if (
                "text" in content_type
                or "json" in content_type
                or "javascript" in content_type
                or "html" in content_type
            ):
                with suppress(Exception):
                    body = await response.text(errors="ignore")
                    numbers.update(extract_wa_numbers(body))

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        logger.warning("Fetch error for %s: %s", url, exc)

    return sorted(numbers), final_url


# ============================================================
# KEYBOARDS
# ============================================================

def kb_main(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="🔍  Scan a Link",
                callback_data="start_scan_prompt",
            )
        ]
    ]

    if is_admin:
        rows.append(
            [
                InlineKeyboardButton(
                    text="👑  Admin Panel",
                    callback_data="admin_panel",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="ℹ️  Help",
                callback_data="help",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Add Number",
                    callback_data="admin_add",
                ),
                InlineKeyboardButton(
                    text="➖ Remove Number",
                    callback_data="admin_remove",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋 View Numbers",
                    callback_data="admin_view_numbers",
                ),
                InlineKeyboardButton(
                    text="🗑️ Clear All",
                    callback_data="admin_clear_all",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Set Min Scans",
                    callback_data="admin_set_min",
                ),
                InlineKeyboardButton(
                    text="⚙️ Set Max Scans",
                    callback_data="admin_set_max",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📊 Show Settings",
                    callback_data="admin_show_settings",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Back",
                    callback_data="back_main",
                )
            ],
        ]
    )


def kb_scan_control(
    user_id: int,
    scanning: bool,
) -> InlineKeyboardMarkup:
    if scanning:
        rows = [
            [
                InlineKeyboardButton(
                    text="⏹️  Stop Scanning",
                    callback_data=f"stop_scan_{user_id}",
                )
            ]
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(
                    text="🔍  Scan Again",
                    callback_data="start_scan_prompt",
                )
            ]
        ]

    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 Main Menu",
                callback_data="back_main",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_results(
    user_id: int,
    hidden: bool,
    has_results: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if has_results:
        label = "🙈  Hide Numbers" if not hidden else "👁️  Show Numbers"

        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"toggle_numbers_{user_id}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="🔍  Scan Again",
                callback_data="start_scan_prompt",
            ),
            InlineKeyboardButton(
                text="🔙 Menu",
                callback_data="back_main",
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# BOT / ROUTER SETUP
# ============================================================

# IMPORTANT:
# aiogram 3.7+ no longer accepts parse_mode="HTML"
# directly in Bot(...).
#
# This is the fix for the Railway crash shown in the screenshot.

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
    ),
)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

router = Router()
dp.include_router(router)


# ============================================================
# HELPERS
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def safe_html(value: str) -> str:
    return html.escape(str(value), quote=False)


def clamp_scan_count(value: int, min_value: int, max_value: int) -> int:
    return max(min_value, min(max_value, value))


def build_scan_buttons(
    min_scans: int,
    max_scans: int,
) -> InlineKeyboardMarkup:
    candidates = [
        min_scans,
        20,
        30,
        50,
        75,
        100,
        max_scans,
    ]

    valid = sorted(
        {
            value
            for value in candidates
            if min_scans <= value <= max_scans
        }
    )

    if not valid:
        valid = [min_scans]

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for scan_count in valid[:8]:
        row.append(
            InlineKeyboardButton(
                text=f"🔢 {scan_count}x",
                callback_data=f"do_scan_{scan_count}",
            )
        )

        if len(row) == 3:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="back_main",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_result_text(
    url: str,
    all_numbers: list[str],
    hidden: bool,
    scans_done,
    scans_total,
    finished: bool,
) -> str:
    unique = sorted(set(all_numbers))
    total = len(unique)

    safe_url = safe_html(url[:60])
    ellipsis = "…" if len(url) > 60 else ""

    header = (
        "╔══════════════════════════════╗\n"
        "║  🔍  <b>WhatsApp Number Scanner</b>  ║\n"
        "╚══════════════════════════════╝\n\n"
    )

    info = (
        f"🔗 <b>Link:</b> <code>{safe_url}{ellipsis}</code>\n"
        f"🔄 <b>Scans done:</b> {scans_done} / {scans_total}\n"
        f"📞 <b>Numbers found:</b> {total} unique\n"
    )

    if finished:
        info += "✅ <b>Status:</b> Scan Complete\n"
    else:
        info += "⏳ <b>Status:</b> Scanning…\n"

    info += "\n"

    if total == 0:
        info += "⚠️ No WhatsApp numbers extracted yet.\n"

    elif hidden:
        info += (
            "🙈 <i>Numbers are hidden. "
            "Tap 👁️ Show Numbers to reveal.</i>\n"
        )

    else:
        info += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        info += "📋 <b>Extracted Numbers:</b>\n\n"

        for index, number in enumerate(unique, 1):
            pretty = number

            if len(number) == 12 and number.startswith("91"):
                pretty = (
                    f"+{number[:2]} "
                    f"{number[2:7]} "
                    f"{number[7:]}"
                )
            elif len(number) == 10:
                pretty = f"{number[:5]} {number[5:]}"

            info += (
                f"  <code>{index:02d}.</code>  "
                f"<b>{safe_html(pretty)}</b>\n"
            )

        info += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

    duplicate_count = max(0, len(all_numbers) - total)

    if duplicate_count > 0:
        info += (
            f"♻️ <i>{duplicate_count} duplicate hit(s) removed</i>\n"
        )

    return header + info


# ============================================================
# SCAN COROUTINE
# ============================================================

async def run_scan(
    user_id: int,
    url: str,
    count: int,
    status_msg: Message,
) -> None:
    collected: list[str] = []

    connector = aiohttp.TCPConnector(
        ssl=False,
        limit=5,
        limit_per_host=2,
    )

    try:
        async with aiohttp.ClientSession(
            connector=connector,
            raise_for_status=False,
        ) as session:

            for scan_index in range(1, count + 1):

                # Stop requested.
                if user_id not in active_scans:
                    break

                numbers, final_url = await fetch_url_and_extract(
                    session,
                    url,
                )

                collected.extend(numbers)

                user_results[user_id]["numbers"] = collected.copy()
                user_results[user_id]["url"] = url
                user_results[user_id]["scans_done"] = scan_index
                user_results[user_id]["scans_total"] = count

                if (
                    scan_index % 5 == 0
                    or scan_index == count
                    or scan_index == 1
                ):
                    hidden = user_results[user_id]["hidden"]

                    text = build_result_text(
                        url=url,
                        all_numbers=collected,
                        hidden=hidden,
                        scans_done=scan_index,
                        scans_total=count,
                        finished=(scan_index == count),
                    )

                    try:
                        await status_msg.edit_text(
                            text,
                            reply_markup=kb_scan_control(
                                user_id,
                                scanning=(scan_index < count),
                            ),
                        )
                    except Exception as exc:
                        logger.debug(
                            "Status edit failed: %s",
                            exc,
                        )

                # Small delay between requests.
                await asyncio.sleep(0.6)

    except asyncio.CancelledError:
        user_results[user_id]["numbers"] = collected.copy()
        user_results[user_id]["stopped"] = True
        raise

    except Exception:
        logger.exception(
            "Unexpected scan error for user %s",
            user_id,
        )

        user_results[user_id]["numbers"] = collected.copy()

        with suppress(Exception):
            await status_msg.edit_text(
                build_result_text(
                    url,
                    collected,
                    user_results[user_id]["hidden"],
                    user_results[user_id]["scans_done"],
                    count,
                    finished=False,
                )
                + "\n\n⚠️ <b>Scan stopped because of an internal error.</b>",
                reply_markup=kb_results(
                    user_id,
                    user_results[user_id]["hidden"],
                    bool(set(collected)),
                ),
            )

    finally:
        # Only remove this user's task if it is the current task.
        current_task = asyncio.current_task()

        if active_scans.get(user_id) is current_task:
            active_scans.pop(user_id, None)

    # If another action removed the task, do not overwrite its state.
    if user_id not in user_results:
        return

    if user_results[user_id].get("stopped"):
        return

    # If loop was stopped manually, the stop handler owns the UI.
    if user_id not in active_scans and user_results[user_id]["scans_done"] < count:
        return

    user_results[user_id]["numbers"] = collected.copy()

    hidden = user_results[user_id]["hidden"]
    unique = sorted(set(collected))

    final_text = build_result_text(
        url=url,
        all_numbers=collected,
        hidden=hidden,
        scans_done=count,
        scans_total=count,
        finished=True,
    )

    with suppress(Exception):
        await status_msg.edit_text(
            final_text,
            reply_markup=kb_results(
                user_id,
                hidden,
                has_results=bool(unique),
            ),
        )


# ============================================================
# /START
# ============================================================

@router.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    name = safe_html(msg.from_user.full_name or "User")

    text = (
        "╔══════════════════════════════╗\n"
        "║  📲  <b>WhatsApp Number Scanner</b>  ║\n"
        "╚══════════════════════════════╝\n\n"
        f"👋 Welcome, <b>{name}</b>!\n\n"
        "🔍 Send a link that you own or are authorized to test.\n"
        "The bot can fetch the link and report WhatsApp numbers "
        "present in its redirect URL or response content.\n\n"
        "⚡ Tap below to get started!"
    )

    await msg.answer(
        text,
        reply_markup=kb_main(is_admin(msg.from_user.id)),
    )


# ============================================================
# /ADMIN
# ============================================================

@router.message(Command("admin"))
async def cmd_admin(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        await msg.answer("❌ Access Denied.")
        return

    await msg.answer(
        "👑 <b>Admin Panel</b>",
        reply_markup=kb_admin(),
    )


# ============================================================
# NAVIGATION
# ============================================================

@router.callback_query(F.data == "back_main")
async def cb_back_main(
    cq: CallbackQuery,
    state: FSMContext,
) -> None:
    await state.clear()

    uid = cq.from_user.id

    if uid in active_scans:
        task = active_scans.pop(uid)
        task.cancel()

    name = safe_html(cq.from_user.full_name or "User")

    text = (
        "╔══════════════════════════════╗\n"
        "║  📲  <b>WhatsApp Number Scanner</b>  ║\n"
        "╚══════════════════════════════╝\n\n"
        f"👋 Welcome back, <b>{name}</b>!\n"
        "Tap 🔍 <b>Scan a Link</b> to begin."
    )

    with suppress(Exception):
        await cq.message.edit_text(
            text,
            reply_markup=kb_main(is_admin(uid)),
        )

    await cq.answer()


@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(cq: CallbackQuery) -> None:
    if not is_admin(cq.from_user.id):
        await cq.answer(
            "❌ Access Denied",
            show_alert=True,
        )
        return

    await cq.message.edit_text(
        "👑 <b>Admin Panel</b>",
        reply_markup=kb_admin(),
    )

    await cq.answer()


@router.callback_query(F.data == "help")
async def cb_help(cq: CallbackQuery) -> None:
    text = (
        "ℹ️ <b>How to use:</b>\n\n"
        "1️⃣ Tap <b>🔍 Scan a Link</b>\n"
        "2️⃣ Send an authorized link\n"
        "3️⃣ Choose the scan count\n"
        "4️⃣ Watch results appear in real time\n"
        "5️⃣ Use <b>👁️ Show/Hide</b> for results\n"
        "6️⃣ Use <b>⏹️ Stop</b> to abort\n\n"
        "🔢 Results are automatically de-duplicated."
    )

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 Back",
                    callback_data="back_main",
                )
            ]
        ]
    )

    await cq.message.edit_text(
        text,
        reply_markup=back_kb,
    )

    await cq.answer()


# ============================================================
# SCAN PROMPT
# ============================================================

@router.callback_query(F.data == "start_scan_prompt")
async def cb_scan_prompt(
    cq: CallbackQuery,
    state: FSMContext,
) -> None:
    await state.set_state(AdminState.waiting_scan_link)

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="back_main",
                )
            ]
        ]
    )

    await cq.message.edit_text(
        "🔗 <b>Send me the link to scan:</b>\n\n"
        "<i>Use only links you own or are authorized to test.</i>",
        reply_markup=back_kb,
    )

    await cq.answer()


@router.message(AdminState.waiting_scan_link)
async def msg_got_link(
    msg: Message,
    state: FSMContext,
) -> None:
    if not msg.text:
        await msg.answer("⚠️ Please send a URL as text.")
        return

    url = msg.text.strip()

    if not re.match(r"^https?://", url, re.IGNORECASE):
        await msg.answer(
            "⚠️ Please send a valid URL starting with "
            "http:// or https://"
        )
        return

    await state.update_data(scan_url=url)

    min_scans = int(await db_get_setting("min_scans"))
    max_scans = int(await db_get_setting("max_scans"))

    await msg.answer(
        "✅ <b>Link received!</b>\n\n"
        f"🔗 <code>{safe_html(url[:70])}</code>\n\n"
        f"📊 <b>Select scan count</b> "
        f"(min {min_scans} · max {max_scans}):",
        reply_markup=build_scan_buttons(
            min_scans,
            max_scans,
        ),
    )

    await state.set_state(None)


# ============================================================
# START SCAN
# ============================================================

@router.callback_query(F.data.startswith("do_scan_"))
async def cb_do_scan(
    cq: CallbackQuery,
    state: FSMContext,
) -> None:
    try:
        count = int(cq.data.split("_")[-1])
    except (ValueError, AttributeError):
        await cq.answer(
            "⚠️ Invalid scan count.",
            show_alert=True,
        )
        return

    min_scans = int(await db_get_setting("min_scans"))
    max_scans = int(await db_get_setting("max_scans"))

    if not min_scans <= count <= max_scans:
        await cq.answer(
            "⚠️ This scan count is no longer allowed.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    url = data.get("scan_url", "")

    if not url:
        await cq.answer(
            "⚠️ No link found. Please try again.",
            show_alert=True,
        )
        await state.clear()
        return

    uid = cq.from_user.id

    # Cancel previous scan for this user.
    old_task = active_scans.pop(uid, None)

    if old_task:
        old_task.cancel()
        with suppress(asyncio.CancelledError):
            await old_task

    user_results[uid] = {
        "numbers": [],
        "hidden": False,
        "url": url,
        "scans_done": 0,
        "scans_total": count,
        "stopped": False,
    }

    status_text = build_result_text(
        url=url,
        all_numbers=[],
        hidden=False,
        scans_done=0,
        scans_total=count,
        finished=False,
    )

    status_msg = await cq.message.edit_text(
        status_text,
        reply_markup=kb_scan_control(
            uid,
            scanning=True,
        ),
    )

    await cq.answer(f"🚀 Starting {count} scans…")

    task = asyncio.create_task(
        run_scan(
            user_id=uid,
            url=url,
            count=count,
            status_msg=status_msg,
        )
    )

    active_scans[uid] = task
    await state.clear()


# ============================================================
# STOP SCAN
# ============================================================

@router.callback_query(F.data.startswith("stop_scan_"))
async def cb_stop_scan(cq: CallbackQuery) -> None:
    try:
        uid = int(cq.data.split("_")[-1])
    except (ValueError, AttributeError):
        await cq.answer(
            "⚠️ Invalid scan.",
            show_alert=True,
        )
        return

    if uid != cq.from_user.id:
        await cq.answer(
            "❌ Not your scan.",
            show_alert=True,
        )
        return

    task = active_scans.pop(uid, None)

    if task:
        user_results[uid]["stopped"] = True
        task.cancel()

        await cq.answer(
            "⏹️ Scan stopped!",
            show_alert=True,
        )

        collected = user_results[uid]["numbers"]
        url = user_results[uid]["url"]
        hidden = user_results[uid]["hidden"]

        text = (
            build_result_text(
                url=url,
                all_numbers=collected,
                hidden=hidden,
                scans_done=user_results[uid]["scans_done"],
                scans_total=user_results[uid]["scans_total"],
                finished=False,
            )
            + "\n\n⏹️ <b>Scan manually stopped.</b>"
        )

        with suppress(Exception):
            await cq.message.edit_text(
                text,
                reply_markup=kb_results(
                    uid,
                    hidden,
                    has_results=bool(set(collected)),
                ),
            )

    else:
        await cq.answer(
            "ℹ️ No active scan.",
            show_alert=True,
        )


# ============================================================
# TOGGLE NUMBERS
# ============================================================

@router.callback_query(F.data.startswith("toggle_numbers_"))
async def cb_toggle_numbers(cq: CallbackQuery) -> None:
    try:
        uid = int(cq.data.split("_")[-1])
    except (ValueError, AttributeError):
        await cq.answer(
            "⚠️ Invalid session.",
            show_alert=True,
        )
        return

    if uid != cq.from_user.id:
        await cq.answer(
            "❌ Not your session.",
            show_alert=True,
        )
        return

    res = user_results[uid]

    res["hidden"] = not res["hidden"]
    hidden = res["hidden"]

    text = build_result_text(
        url=res["url"],
        all_numbers=res["numbers"],
        hidden=hidden,
        scans_done=res["scans_done"],
        scans_total=res["scans_total"],
        finished=True,
    )

    unique = sorted(set(res["numbers"]))

    await cq.message.edit_text(
        text,
        reply_markup=kb_results(
            uid,
            hidden,
            has_results=bool(unique),
        ),
    )

    await cq.answer(
        "🙈 Hidden!" if hidden else "👁️ Shown!"
    )


# ============================================================
# ADMIN — VIEW NUMBERS
# ============================================================

@router.callback_query(F.data == "admin_view_numbers")
async def cb_admin_view_numbers(cq: CallbackQuery) -> None:
    if not is_admin(cq.from_user.id):
        await cq.answer(
            "❌ Access Denied",
            show_alert=True,
        )
        return

    numbers = await db_get_all_numbers()

    if not numbers:
        text = "📭 <b>No numbers stored yet.</b>"
    else:
        lines = "\n".join(
            f"  <code>{index:02d}.</code> "
            f"<b>{safe_html(number)}</b>"
            for index, number in enumerate(numbers, 1)
        )

        text = (
            f"📋 <b>Stored Numbers ({len(numbers)}):</b>\n\n"
            f"{lines}"
        )

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 Admin",
                    callback_data="admin_panel",
                )
            ]
        ]
    )

    await cq.message.edit_text(
        text,
        reply_markup=back_kb,
    )

    await cq.answer()


# ============================================================
# ADMIN — ADD NUMBER
# ============================================================

@router.callback_query(F.data == "admin_add")
async def cb_admin_add(
    cq: CallbackQuery,
    state: FSMContext,
) -> None:
    if not is_admin(cq.from_user.id):
        await cq.answer(
            "❌ Access Denied",
            show_alert=True,
        )
        return

    await state.set_state(AdminState.waiting_add_number)

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="admin_panel",
                )
            ]
        ]
    )

    await cq.message.edit_text(
        "➕ <b>Send the WhatsApp number to add:</b>\n\n"
        "<i>Format: 919876543210 "
        "(country code, digits only)</i>",
        reply_markup=back_kb,
    )

    await cq.answer()


@router.message(AdminState.waiting_add_number)
async def msg_add_number(
    msg: Message,
    state: FSMContext,
) -> None:
    if not msg.text:
        await msg.answer("⚠️ Send a number.")
        return

    raw = re.sub(r"\D", "", msg.text.strip())

    if not 7 <= len(raw) <= 15:
        await msg.answer(
            "⚠️ Invalid number. "
            "Please send 7–15 digits."
        )
        return

    ok = await db_add_number(raw)

    await state.clear()

    if ok:
        await msg.answer(
            f"✅ <b>{safe_html(raw)}</b> added successfully!",
            reply_markup=kb_admin(),
        )
    else:
        await msg.answer(
            f"⚠️ <b>{safe_html(raw)}</b> already exists.",
            reply_markup=kb_admin(),
        )


# ============================================================
# ADMIN — REMOVE NUMBER
# ============================================================

@router.callback_query(F.data == "admin_remove")
async def cb_admin_remove(
    cq: CallbackQuery,
    state: FSMContext,
) -> None:
    if not is_admin(cq.from_user.id):
        await cq.answer(
            "❌ Access Denied",
            show_alert=True,
        )
        return

    numbers = await db_get_all_numbers()

    if not numbers:
        await cq.answer(
            "📭 No numbers to remove.",
            show_alert=True,
        )
        return

    await state.set_state(AdminState.waiting_remove_number)

    lines = "\n".join(
        f"  {index}. {safe_html(number)}"
        for index, number in enumerate(numbers, 1)
    )

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="admin_panel",
                )
            ]
        ]
    )

    await cq.message.edit_text(
        "➖ <b>Send the number to remove:</b>\n\n"
        f"{lines}\n\n"
        "<i>Send the exact number.</i>",
        reply_markup=back_kb,
    )

    await cq.answer()


@router.message(AdminState.waiting_remove_number)
async def msg_remove_number(
    msg: Message,
    state: FSMContext,
) -> None:
    if not msg.text:
        await msg.answer("⚠️ Send a number.")
        return

    raw = re.sub(r"\D", "", msg.text.strip())

    if not 7 <= len(raw) <= 15:
        await msg.answer(
            "⚠️ Invalid number. Please send 7–15 digits."
        )
        return

    ok = await db_remove_number(raw)

    await state.clear()

    if ok:
        await msg.answer(
            f"✅ <b>{safe_html(raw)}</b> removed!",
            reply_markup=kb_admin(),
        )
    else:
        await msg.answer(
            f"⚠️ <b>{safe_html(raw)}</b> not found.",
            reply_markup=kb_admin(),
        )


# ============================================================
# ADMIN — CLEAR ALL
# ============================================================

@router.callback_query(F.data == "admin_clear_all")
async def cb_admin_clear_all(cq: CallbackQuery) -> None:
    if not is_admin(cq.from_user.id):
        await cq.answer(
            "❌ Access Denied",
            show_alert=True,
        )
        return

    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Yes, Clear All",
                    callback_data="admin_clear_confirm",
                ),
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="admin_panel",
                ),
            ]
        ]
    )

    await cq.message.edit_text(
        "⚠️ <b>Are you sure you want to clear "
        "ALL stored numbers?</b>",
        reply_markup=confirm_kb,
    )

    await cq.answer()


@router.callback_query(F.data == "admin_clear_confirm")
async def cb_admin_clear_confirm(
    cq: CallbackQuery,
) -> None:
    if not is_admin(cq.from_user.id):
        await cq.answer(
            "❌ Access Denied",
            show_alert=True,
        )
        return

    await db_clear_numbers()

    await cq.message.edit_text(
        "🗑️ <b>All numbers cleared!</b>",
        reply_markup=kb_admin(),
    )

    await cq.answer("✅ Cleared!")


# ============================================================
# ADMIN — SET MIN SCANS
# ============================================================

@router.callback_query(F.data == "admin_set_min")
async def cb_set_min(
    cq: CallbackQuery,
    state: FSMContext,
) -> None:
    if not is_admin(cq.from_user.id):
        await cq.answer(
            "❌ Access Denied",
            show_alert=True,
        )
        return

    await state.set_state(AdminState.waiting_set_min)

    current = await db_get_setting("min_scans")

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="admin_panel",
                )
            ]
        ]
    )

    await cq.message.edit_text(
        "⚙️ <b>Set Minimum Scans</b>\n\n"
        f"Current: <b>{current}</b>\n\n"
        "Send a number (1–499):",
        reply_markup=back_kb,
    )

    await cq.answer()


@router.message(AdminState.waiting_set_min)
async def msg_set_min(
    msg: Message,
    state: FSMContext,
) -> None:
    if not msg.text:
        await msg.answer("⚠️ Send a number.")
        return

    try:
        value = int(msg.text.strip())

        if not 1 <= value <= 499:
            raise ValueError

    except ValueError:
        await msg.answer(
            "⚠️ Send a number between 1 and 499."
        )
        return

    max_scans = int(await db_get_setting("max_scans"))

    if value >= max_scans:
        await msg.answer(
            f"⚠️ Min ({value}) must be less than "
            f"Max ({max_scans})."
        )
        return

    await db_set_setting("min_scans", str(value))
    await state.clear()

    await msg.answer(
        f"✅ Minimum scans set to <b>{value}</b>!",
        reply_markup=kb_admin(),
    )


# ============================================================
# ADMIN — SET MAX SCANS
# ============================================================

@router.callback_query(F.data == "admin_set_max")
async def cb_set_max(
    cq: CallbackQuery,
    state: FSMContext,
) -> None:
    if not is_admin(cq.from_user.id):
        await cq.answer(
            "❌ Access Denied",
            show_alert=True,
        )
        return

    await state.set_state(AdminState.waiting_set_max)

    current = await db_get_setting("max_scans")

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="admin_panel",
                )
            ]
        ]
    )

    await cq.message.edit_text(
        "⚙️ <b>Set Maximum Scans</b>\n\n"
        f"Current: <b>{current}</b>\n\n"
        "Send a number (2–500):",
        reply_markup=back_kb,
    )

    await cq.answer()


@router.message(AdminState.waiting_set_max)
async def msg_set_max(
    msg: Message,
    state: FSMContext,
) -> None:
    if not msg.text:
        await msg.answer("⚠️ Send a number.")
        return

    try:
        value = int(msg.text.strip())

        if not 2 <= value <= 500:
            raise ValueError

    except ValueError:
        await msg.answer(
            "⚠️ Send a number between 2 and 500."
        )
        return

    min_scans = int(await db_get_setting("min_scans"))

    if value <= min_scans:
        await msg.answer(
            f"⚠️ Max ({value}) must be greater than "
            f"Min ({min_scans})."
        )
        return

    await db_set_setting("max_scans", str(value))
    await state.clear()

    await msg.answer(
        f"✅ Maximum scans set to <b>{value}</b>!",
        reply_markup=kb_admin(),
    )


# ============================================================
# ADMIN — SHOW SETTINGS
# ============================================================

@router.callback_query(F.data == "admin_show_settings")
async def cb_show_settings(cq: CallbackQuery) -> None:
    if not is_admin(cq.from_user.id):
        await cq.answer(
            "❌ Access Denied",
            show_alert=True,
        )
        return

    min_scans = await db_get_setting("min_scans")
    max_scans = await db_get_setting("max_scans")
    numbers = await db_get_all_numbers()

    text = (
        "📊 <b>Current Settings</b>\n\n"
        f"🔽 Min Scans : <b>{min_scans}</b>\n"
        f"🔼 Max Scans : <b>{max_scans}</b>\n"
        f"📞 Numbers DB: <b>{len(numbers)}</b> stored\n"
    )

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 Admin",
                    callback_data="admin_panel",
                )
            ]
        ]
    )

    await cq.message.edit_text(
        text,
        reply_markup=back_kb,
    )

    await cq.answer()


# ============================================================
# FALLBACK — URL MESSAGE
# ============================================================

@router.message(F.text)
async def msg_catch_url(
    msg: Message,
    state: FSMContext,
) -> None:
    text = msg.text.strip()

    if re.match(r"^https?://", text, re.IGNORECASE):
        await state.update_data(scan_url=text)

        min_scans = int(await db_get_setting("min_scans"))
        max_scans = int(await db_get_setting("max_scans"))

        await msg.answer(
            "🔗 <b>Link detected</b>\n\n"
            f"<code>{safe_html(text[:80])}</code>\n\n"
            f"📊 <b>Choose scan count</b> "
            f"(min {min_scans} · max {max_scans}):",
            reply_markup=build_scan_buttons(
                min_scans,
                max_scans,
            ),
        )

    else:
        await msg.answer(
            "👋 Use /start to access the scanner.",
            reply_markup=kb_main(
                is_admin(msg.from_user.id)
            ),
        )


# ============================================================
# ERROR HANDLER
# ============================================================

@router.errors()
async def error_handler(event) -> bool:
    logger.exception(
        "Unhandled update error: %s",
        event.exception,
    )
    return True


# ============================================================
# ENTRY POINT
# ============================================================

async def main() -> None:
    await init_db()

    logger.info("========================================")
    logger.info("🚀 Bot starting...")
    logger.info("aiogram compatibility: 3.7+")
    logger.info("Database: %s", DB_PATH)
    logger.info("Admins configured: %d", len(ADMIN_IDS))
    logger.info("========================================")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        # Cancel any remaining scan tasks.
        tasks = list(active_scans.values())
        active_scans.clear()

        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
