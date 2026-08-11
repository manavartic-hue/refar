import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from typing import Optional

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS   = list(map(int, os.getenv("ADMIN_IDS", "123456789").split(",")))
DB_PATH     = "scanner.db"

DEFAULT_MIN_SCANS = 20
DEFAULT_MAX_SCANS = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("WA-Scanner")

# ─────────────────────────────────────────────
#  FSM STATES
# ─────────────────────────────────────────────
class AdminState(StatesGroup):
    waiting_add_number    = State()
    waiting_remove_number = State()
    waiting_set_min       = State()
    waiting_set_max       = State()
    waiting_scan_link     = State()


# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wa_numbers (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                number  TEXT UNIQUE NOT NULL,
                added_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Default settings
        await db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
            ("min_scans", str(DEFAULT_MIN_SCANS))
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
            ("max_scans", str(DEFAULT_MAX_SCANS))
        )
        await db.commit()


async def db_get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""


async def db_set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (key, value)
        )
        await db.commit()


async def db_add_number(number: str) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO wa_numbers(number) VALUES(?)", (number,))
            await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def db_remove_number(number: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM wa_numbers WHERE number=?", (number,))
        await db.commit()
        return cur.rowcount > 0


async def db_get_all_numbers() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT number FROM wa_numbers ORDER BY id") as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


# ─────────────────────────────────────────────
#  ACTIVE SCAN SESSIONS  {user_id: task}
# ─────────────────────────────────────────────
active_scans: dict[int, asyncio.Task] = {}

# ─────────────────────────────────────────────
#  WHATSAPP NUMBER EXTRACTOR
# ─────────────────────────────────────────────
WA_PATTERNS = [
    # wa.me/919876543210
    r"wa\.me/(\d{7,15})",
    # api.whatsapp.com/send?phone=91...
    r"api\.whatsapp\.com/send\?phone=(\d{7,15})",
    # whatsapp://send?phone=91...
    r"whatsapp://send\?phone=(\d{7,15})",
    # +91-98765-43210  or  +919876543210
    r"\+(\d{1,3}[\s\-]?\d{5,12})",
    # raw 10-digit Indian mobile
    r"\b(91\d{10}|\d{10})\b",
    # href="tel:+91..."
    r"tel:\+?(\d{7,15})",
]

def extract_wa_numbers(text: str) -> list[str]:
    found = set()
    for pat in WA_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            raw = re.sub(r"[\s\-]", "", m.group(1))
            # normalise – keep only digit strings
            digits = re.sub(r"\D", "", raw)
            if len(digits) >= 7:
                found.add(digits)
    return sorted(found)


async def fetch_url_and_extract(
    session: aiohttp.ClientSession,
    url: str,
) -> tuple[list[str], str | None]:
    """
    Visit URL (follow redirects), extract WhatsApp numbers from:
      1. Final redirect URL
      2. Response body HTML/text
    Returns (numbers_found, final_url)
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    numbers: list[str] = []
    final_url: str | None = None
    try:
        async with session.get(
            url, headers=headers, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=15),
            ssl=False,
        ) as resp:
            final_url = str(resp.url)
            # numbers from redirect URL itself
            numbers += extract_wa_numbers(final_url)
            try:
                body = await resp.text(errors="ignore")
                numbers += extract_wa_numbers(body)
            except Exception:
                pass
    except Exception as e:
        logger.warning("Fetch error for %s: %s", url, e)
    return list(set(numbers)), final_url


# ─────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────
def kb_main(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔍  Scan a Link", callback_data="start_scan_prompt")],
    ]
    if is_admin:
        rows.append([
            InlineKeyboardButton(text="👑  Admin Panel", callback_data="admin_panel"),
        ])
    rows.append([
        InlineKeyboardButton(text="ℹ️  Help", callback_data="help"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Add Number",    callback_data="admin_add"),
            InlineKeyboardButton(text="➖ Remove Number", callback_data="admin_remove"),
        ],
        [
            InlineKeyboardButton(text="📋 View Numbers",  callback_data="admin_view_numbers"),
            InlineKeyboardButton(text="🗑️ Clear All",     callback_data="admin_clear_all"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Set Min Scans", callback_data="admin_set_min"),
            InlineKeyboardButton(text="⚙️ Set Max Scans", callback_data="admin_set_max"),
        ],
        [
            InlineKeyboardButton(text="📊 Show Settings", callback_data="admin_show_settings"),
        ],
        [
            InlineKeyboardButton(text="🔙 Back",          callback_data="back_main"),
        ],
    ])


def kb_scan_control(user_id: int, scanning: bool, count: int | None = None) -> InlineKeyboardMarkup:
    rows = []
    if scanning:
        rows.append([
            InlineKeyboardButton(text="⏹️  Stop Scanning", callback_data=f"stop_scan_{user_id}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="🔍  Scan Again",    callback_data="start_scan_prompt"),
        ])
    rows.append([
        InlineKeyboardButton(text="🔙 Main Menu",          callback_data="back_main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_results(user_id: int, hidden: bool, has_results: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_results:
        label = "🙈  Hide Numbers" if not hidden else "👁️  Show Numbers"
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"toggle_numbers_{user_id}"),
        ])
    rows.append([
        InlineKeyboardButton(text="🔍  Scan Again", callback_data="start_scan_prompt"),
        InlineKeyboardButton(text="🔙 Menu",        callback_data="back_main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─────────────────────────────────────────────
#  BOT / ROUTER SETUP
# ─────────────────────────────────────────────
bot        = Bot(token=BOT_TOKEN, parse_mode="HTML")
storage    = MemoryStorage()
dp         = Dispatcher(storage=storage)
router     = Router()
dp.include_router(router)

# per-user result storage  {user_id: {"numbers": [...], "hidden": bool, "url": str}}
user_results: dict[int, dict] = defaultdict(lambda: {"numbers": [], "hidden": False, "url": ""})


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ─────────────────────────────────────────────
#  HELPER: build result text
# ─────────────────────────────────────────────
def build_result_text(
    url: str,
    all_numbers: list[str],
    hidden: bool,
    scans_done: int,
    scans_total: int,
    finished: bool,
) -> str:
    unique = sorted(set(all_numbers))
    total  = len(unique)

    header = (
        "╔══════════════════════════════╗\n"
        "║  🔍  <b>WhatsApp Number Scanner</b>  ║\n"
        "╚══════════════════════════════╝\n\n"
    )
    info = (
        f"🔗 <b>Link:</b> <code>{url[:60]}{'…' if len(url)>60 else ''}</code>\n"
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
        info += "🙈 <i>Numbers are hidden. Tap 👁️ Show Numbers to reveal.</i>\n"
    else:
        info += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        info += "📋 <b>Extracted Numbers:</b>\n\n"
        for i, num in enumerate(unique, 1):
            # prettify: if 12 digits starting with 91 → +91 XXXXX XXXXX
            pretty = num
            if len(num) == 12 and num.startswith("91"):
                pretty = f"+{num[:2]} {num[2:7]} {num[7:]}"
            elif len(num) == 10:
                pretty = f"{num[:5]} {num[5:]}"
            info += f"  <code>{i:02d}.</code>  <b>{pretty}</b>\n"
        info += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

    # duplicates stats
    dup_count = len(all_numbers) - total
    if dup_count > 0:
        info += f"♻️  <i>{dup_count} duplicate hit(s) removed</i>\n"

    return header + info


# ─────────────────────────────────────────────
#  SCAN COROUTINE
# ─────────────────────────────────────────────
async def run_scan(
    user_id: int,
    url: str,
    count: int,
    status_msg: Message,
):
    collected: list[str] = []
    connector = aiohttp.TCPConnector(ssl=False, limit=5)

    async with aiohttp.ClientSession(connector=connector) as session:
        for i in range(1, count + 1):
            # Cancelled?
            if user_id not in active_scans:
                break

            nums, final_url = await fetch_url_and_extract(session, url)
            collected.extend(nums)

            # update every 5 scans or on last
            if i % 5 == 0 or i == count or i == 1:
                user_results[user_id]["numbers"] = collected
                user_results[user_id]["url"]     = url
                hidden = user_results[user_id]["hidden"]
                text   = build_result_text(url, collected, hidden, i, count, finished=(i == count))
                try:
                    await status_msg.edit_text(
                        text,
                        reply_markup=kb_scan_control(user_id, scanning=(i < count)),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            # small delay so we don't hammer the server
            await asyncio.sleep(0.6)

    # Finalize
    active_scans.pop(user_id, None)
    user_results[user_id]["numbers"] = collected
    hidden = user_results[user_id]["hidden"]
    unique = sorted(set(collected))
    final_text = build_result_text(url, collected, hidden, count, count, finished=True)
    try:
        await status_msg.edit_text(
            final_text,
            reply_markup=kb_results(user_id, hidden, has_results=bool(unique)),
            parse_mode="HTML",
        )
    except Exception:
        pass


# ─────────────────────────────────────────────
#  HANDLERS — /start
# ─────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(msg: Message):
    name = msg.from_user.full_name or "User"
    text = (
        "╔══════════════════════════════╗\n"
        "║  📲  <b>WhatsApp Number Scanner</b>  ║\n"
        "╚══════════════════════════════╝\n\n"
        f"👋 Welcome, <b>{name}</b>!\n\n"
        "🔍 Paste any link and I'll <b>scan it multiple times</b>,\n"
        "     extract every WhatsApp number it redirects to,\n"
        "     and give you a clean, de-duplicated list.\n\n"
        "⚡ Tap below to get started!"
    )
    await msg.answer(text, reply_markup=kb_main(is_admin(msg.from_user.id)))


# ─────────────────────────────────────────────
#  HANDLERS — /admin
# ─────────────────────────────────────────────
@router.message(Command("admin"))
async def cmd_admin(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("❌ Access Denied.")
        return
    await msg.answer("👑 <b>Admin Panel</b>", reply_markup=kb_admin())


# ─────────────────────────────────────────────
#  CALLBACKS — navigation
# ─────────────────────────────────────────────
@router.callback_query(F.data == "back_main")
async def cb_back_main(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    name = cq.from_user.full_name or "User"
    text = (
        "╔══════════════════════════════╗\n"
        "║  📲  <b>WhatsApp Number Scanner</b>  ║\n"
        "╚══════════════════════════════╝\n\n"
        f"👋 Welcome back, <b>{name}</b>!\n"
        "Tap 🔍 <b>Scan a Link</b> to begin."
    )
    await cq.message.edit_text(text, reply_markup=kb_main(is_admin(cq.from_user.id)))
    await cq.answer()


@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied", show_alert=True)
        return
    await cq.message.edit_text("👑 <b>Admin Panel</b>", reply_markup=kb_admin())
    await cq.answer()


@router.callback_query(F.data == "help")
async def cb_help(cq: CallbackQuery):
    text = (
        "ℹ️ <b>How to use:</b>\n\n"
        "1️⃣ Tap <b>🔍 Scan a Link</b>\n"
        "2️⃣ Send the WhatsApp rotation link\n"
        "3️⃣ Choose how many scans (20–100)\n"
        "4️⃣ Watch numbers appear in real time\n"
        "5️⃣ Use <b>👁️ Show/Hide</b> to toggle number visibility\n"
        "6️⃣ Use <b>⏹️ Stop</b> anytime to abort\n\n"
        "🔢 Numbers are auto de-duplicated and prettified."
    )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Back", callback_data="back_main")
    ]])
    await cq.message.edit_text(text, reply_markup=back_kb)
    await cq.answer()


# ─────────────────────────────────────────────
#  CALLBACKS — scan prompt
# ─────────────────────────────────────────────
@router.callback_query(F.data == "start_scan_prompt")
async def cb_scan_prompt(cq: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_scan_link)
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Cancel", callback_data="back_main")
    ]])
    await cq.message.edit_text(
        "🔗 <b>Send me the link to scan:</b>\n\n"
        "<i>Example: https://wa.me/91xxxxxxxxxx or any rotation link</i>",
        reply_markup=back_kb,
    )
    await cq.answer()


@router.message(AdminState.waiting_scan_link)
async def msg_got_link(msg: Message, state: FSMContext):
    url = msg.text.strip()
    if not url.startswith("http"):
        await msg.answer("⚠️ Please send a valid URL starting with http:// or https://")
        return

    await state.update_data(scan_url=url)

    min_s = int(await db_get_setting("min_scans"))
    max_s = int(await db_get_setting("max_scans"))

    # Build scan count buttons
    steps = []
    if min_s <= 20 <= max_s: steps.append(20)
    if min_s <= 30 <= max_s: steps.append(30)
    if min_s <= 50 <= max_s: steps.append(50)
    if min_s <= 75 <= max_s: steps.append(75)
    if max_s not in steps:   steps.append(max_s)
    steps = sorted(set(steps))

    rows = []
    row  = []
    for s in steps:
        row.append(InlineKeyboardButton(text=f"🔢 {s}x", callback_data=f"do_scan_{s}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="back_main")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await msg.answer(
        f"✅ Link received!\n\n"
        f"🔗 <code>{url[:70]}</code>\n\n"
        f"📊 <b>Select scan count</b> (min {min_s} · max {max_s}):",
        reply_markup=kb,
    )
    await state.set_state(None)


@router.callback_query(F.data.startswith("do_scan_"))
async def cb_do_scan(cq: CallbackQuery, state: FSMContext):
    count = int(cq.data.split("_")[-1])
    data  = await state.get_data()
    url   = data.get("scan_url", "")

    if not url:
        await cq.answer("⚠️ No link found. Please try again.", show_alert=True)
        await state.clear()
        return

    # Cancel any existing scan for this user
    if cq.from_user.id in active_scans:
        active_scans[cq.from_user.id].cancel()
        active_scans.pop(cq.from_user.id, None)

    user_results[cq.from_user.id] = {"numbers": [], "hidden": False, "url": url}

    status_text = build_result_text(url, [], False, 0, count, finished=False)
    status_msg  = await cq.message.edit_text(
        status_text,
        reply_markup=kb_scan_control(cq.from_user.id, scanning=True),
        parse_mode="HTML",
    )
    await cq.answer(f"🚀 Starting {count} scans…")

    task = asyncio.create_task(
        run_scan(cq.from_user.id, url, count, status_msg)
    )
    active_scans[cq.from_user.id] = task


# ─────────────────────────────────────────────
#  CALLBACKS — stop scan
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("stop_scan_"))
async def cb_stop_scan(cq: CallbackQuery):
    uid = int(cq.data.split("_")[-1])
    if uid != cq.from_user.id:
        await cq.answer("❌ Not your scan.", show_alert=True)
        return

    if uid in active_scans:
        active_scans[uid].cancel()
        active_scans.pop(uid, None)
        await cq.answer("⏹️ Scan stopped!", show_alert=True)

        collected = user_results[uid]["numbers"]
        url       = user_results[uid]["url"]
        hidden    = user_results[uid]["hidden"]
        text = (
            build_result_text(url, collected, hidden, "?", "?", finished=False)
            + "\n\n⏹️ <b>Scan manually stopped.</b>"
        )
        await cq.message.edit_text(
            text,
            reply_markup=kb_results(uid, hidden, has_results=bool(set(collected))),
            parse_mode="HTML",
        )
    else:
        await cq.answer("ℹ️ No active scan.", show_alert=True)


# ─────────────────────────────────────────────
#  CALLBACKS — toggle numbers
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("toggle_numbers_"))
async def cb_toggle_numbers(cq: CallbackQuery):
    uid = int(cq.data.split("_")[-1])
    if uid != cq.from_user.id:
        await cq.answer("❌ Not your session.", show_alert=True)
        return

    res    = user_results[uid]
    hidden = not res["hidden"]
    user_results[uid]["hidden"] = hidden

    text = build_result_text(
        res["url"], res["numbers"], hidden,
        "—", "—", finished=True
    )
    unique = sorted(set(res["numbers"]))
    await cq.message.edit_text(
        text,
        reply_markup=kb_results(uid, hidden, has_results=bool(unique)),
        parse_mode="HTML",
    )
    await cq.answer("🙈 Hidden!" if hidden else "👁️ Shown!")


# ─────────────────────────────────────────────
#  ADMIN — view numbers
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_view_numbers")
async def cb_admin_view_numbers(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied", show_alert=True)
        return
    numbers = await db_get_all_numbers()
    if not numbers:
        text = "📭 <b>No numbers stored yet.</b>"
    else:
        lines = "\n".join(f"  <code>{i:02d}.</code> <b>{n}</b>" for i, n in enumerate(numbers, 1))
        text  = f"📋 <b>Stored Numbers ({len(numbers)}):</b>\n\n{lines}"
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Admin", callback_data="admin_panel")
    ]])
    await cq.message.edit_text(text, reply_markup=back_kb)
    await cq.answer()


# ─────────────────────────────────────────────
#  ADMIN — add number
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_add")
async def cb_admin_add(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied", show_alert=True)
        return
    await state.set_state(AdminState.waiting_add_number)
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Cancel", callback_data="admin_panel")
    ]])
    await cq.message.edit_text(
        "➕ <b>Send the WhatsApp number to add:</b>\n\n"
        "<i>Format: 919876543210 (with country code, no +)</i>",
        reply_markup=back_kb,
    )
    await cq.answer()


@router.message(AdminState.waiting_add_number)
async def msg_add_number(msg: Message, state: FSMContext):
    raw    = re.sub(r"\D", "", msg.text.strip())
    if len(raw) < 7:
        await msg.answer("⚠️ Invalid number. Please send digits only (7–15 digits).")
        return
    ok = await db_add_number(raw)
    await state.clear()
    if ok:
        await msg.answer(f"✅ <b>{raw}</b> added successfully!", reply_markup=kb_admin())
    else:
        await msg.answer(f"⚠️ <b>{raw}</b> already exists in the list.", reply_markup=kb_admin())


# ─────────────────────────────────────────────
#  ADMIN — remove number
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_remove")
async def cb_admin_remove(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied", show_alert=True)
        return
    await state.set_state(AdminState.waiting_remove_number)
    numbers = await db_get_all_numbers()
    if not numbers:
        await cq.answer("📭 No numbers to remove.", show_alert=True)
        return
    lines = "\n".join(f"  {i}. {n}" for i, n in enumerate(numbers, 1))
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Cancel", callback_data="admin_panel")
    ]])
    await cq.message.edit_text(
        f"➖ <b>Send the number to remove:</b>\n\n{lines}\n\n"
        "<i>Send the exact number (digits only)</i>",
        reply_markup=back_kb,
    )
    await cq.answer()


@router.message(AdminState.waiting_remove_number)
async def msg_remove_number(msg: Message, state: FSMContext):
    raw = re.sub(r"\D", "", msg.text.strip())
    ok  = await db_remove_number(raw)
    await state.clear()
    if ok:
        await msg.answer(f"✅ <b>{raw}</b> removed!", reply_markup=kb_admin())
    else:
        await msg.answer(f"⚠️ <b>{raw}</b> not found in list.", reply_markup=kb_admin())


# ─────────────────────────────────────────────
#  ADMIN — clear all
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_clear_all")
async def cb_admin_clear_all(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied", show_alert=True)
        return
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Yes, Clear All", callback_data="admin_clear_confirm"),
            InlineKeyboardButton(text="❌ Cancel",          callback_data="admin_panel"),
        ]
    ])
    await cq.message.edit_text(
        "⚠️ <b>Are you sure you want to clear ALL stored numbers?</b>",
        reply_markup=confirm_kb,
    )
    await cq.answer()


@router.callback_query(F.data == "admin_clear_confirm")
async def cb_admin_clear_confirm(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM wa_numbers")
        await db.commit()
    await cq.message.edit_text("🗑️ <b>All numbers cleared!</b>", reply_markup=kb_admin())
    await cq.answer("✅ Cleared!")


# ─────────────────────────────────────────────
#  ADMIN — set min/max scans
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_set_min")
async def cb_set_min(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied", show_alert=True)
        return
    await state.set_state(AdminState.waiting_set_min)
    cur = await db_get_setting("min_scans")
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Cancel", callback_data="admin_panel")
    ]])
    await cq.message.edit_text(
        f"⚙️ <b>Set Minimum Scans</b>\n\nCurrent: <b>{cur}</b>\n\nSend a number (1–99):",
        reply_markup=back_kb,
    )
    await cq.answer()


@router.message(AdminState.waiting_set_min)
async def msg_set_min(msg: Message, state: FSMContext):
    try:
        val = int(msg.text.strip())
        assert 1 <= val <= 99
    except Exception:
        await msg.answer("⚠️ Send a number between 1 and 99.")
        return
    max_s = int(await db_get_setting("max_scans"))
    if val >= max_s:
        await msg.answer(f"⚠️ Min ({val}) must be less than Max ({max_s}).")
        return
    await db_set_setting("min_scans", str(val))
    await state.clear()
    await msg.answer(f"✅ Minimum scans set to <b>{val}</b>!", reply_markup=kb_admin())


@router.callback_query(F.data == "admin_set_max")
async def cb_set_max(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied", show_alert=True)
        return
    await state.set_state(AdminState.waiting_set_max)
    cur = await db_get_setting("max_scans")
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Cancel", callback_data="admin_panel")
    ]])
    await cq.message.edit_text(
        f"⚙️ <b>Set Maximum Scans</b>\n\nCurrent: <b>{cur}</b>\n\nSend a number (2–500):",
        reply_markup=back_kb,
    )
    await cq.answer()


@router.message(AdminState.waiting_set_max)
async def msg_set_max(msg: Message, state: FSMContext):
    try:
        val = int(msg.text.strip())
        assert 2 <= val <= 500
    except Exception:
        await msg.answer("⚠️ Send a number between 2 and 500.")
        return
    min_s = int(await db_get_setting("min_scans"))
    if val <= min_s:
        await msg.answer(f"⚠️ Max ({val}) must be greater than Min ({min_s}).")
        return
    await db_set_setting("max_scans", str(val))
    await state.clear()
    await msg.answer(f"✅ Maximum scans set to <b>{val}</b>!", reply_markup=kb_admin())


# ─────────────────────────────────────────────
#  ADMIN — show settings
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin_show_settings")
async def cb_show_settings(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied", show_alert=True)
        return
    min_s   = await db_get_setting("min_scans")
    max_s   = await db_get_setting("max_scans")
    numbers = await db_get_all_numbers()
    text = (
        "📊 <b>Current Settings</b>\n\n"
        f"🔽 Min Scans : <b>{min_s}</b>\n"
        f"🔼 Max Scans : <b>{max_s}</b>\n"
        f"📞 Numbers DB: <b>{len(numbers)}</b> stored\n"
    )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Admin", callback_data="admin_panel")
    ]])
    await cq.message.edit_text(text, reply_markup=back_kb)
    await cq.answer()


# ─────────────────────────────────────────────
#  FALLBACK — any text that looks like a URL
# ─────────────────────────────────────────────
@router.message(F.text)
async def msg_catch_url(msg: Message, state: FSMContext):
    text = msg.text.strip()
    if text.startswith("http"):
        # treat as link → ask count
        await state.update_data(scan_url=text)
        min_s = int(await db_get_setting("min_scans"))
        max_s = int(await db_get_setting("max_scans"))
        steps = sorted({min_s, 20, 30, 50, 75, max_s} & set(range(min_s, max_s + 1)))
        rows, row = [], []
        for s in steps[:6]:
            row.append(InlineKeyboardButton(text=f"🔢 {s}x", callback_data=f"do_scan_{s}"))
            if len(row) == 3:
                rows.append(row); row = []
        if row: rows.append(row)
        rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="back_main")])
        await msg.answer(
            f"🔗 Link detected:\n<code>{text[:80]}</code>\n\n"
            f"📊 <b>Choose scan count</b> (min {min_s} · max {max_s}):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    else:
        await msg.answer(
            "👋 Use /start to access the scanner.",
            reply_markup=kb_main(is_admin(msg.from_user.id)),
        )


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
async def main():
    await init_db()
    logger.info("🚀 Bot starting…")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
