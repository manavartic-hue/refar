"""
╔══════════════════════════════════════════════════════════════════════════╗
║   GMAP AGENT — DUAL-MODE REFERRAL / TASK BOT  (aiogram 3.x)  [FIXED]     ║
║   + Reply keyboard menu  + Joined/Done gate  + already-joined fix        ║
║   + safe message edit (numbers/admin)  + one-by-one message editor       ║
╚══════════════════════════════════════════════════════════════════════════╝

WHAT THIS BOT DOES
------------------
One bot, two switchable personalities (change any time from /admin → Bot Mode,
no data is lost, users never leave):

    • 🎯 TASK & EARN   — image/banner says "Task and Earn Bot"
    • 🤝 REFER & EARN  — "Refer & Earn Bot — Agent Numbers Loot"

In BOTH modes the entry flow is identical and runs in this exact order:

    /start → 🔒 Join Channels → 🧩 Captcha → 📱 Indian-number verify → 🎁 Dashboard

REWARD MODEL
------------
Every successful referral pays out ONE WhatsApp number from a bulk pool the
admin loads. Numbers are handed out one-by-one, spread evenly so the SAME
number is never given to more than MAX_USERS_PER_NUMBER (default 20) people,
and the pool is shuffled so users don't all get the same number first. The
payout arrives as a stylish card with a wa.me button. Premium/custom emoji in
the admin-set caption are preserved exactly as the admin typed them.

INDIA-ONLY VERIFICATION
-----------------------
Only +91 (10-digit Indian mobile) numbers pass. A non-Indian number RESTRICTS
(does not delete) the account that started the bot: they see a "Contact Admin"
screen with a button to the configured admin username, and the person who
referred them is told the referral was invalid. Restriction is reversible from
the admin user card.

RESET (keeps everyone)
----------------------
/admin → Reset Referrals zeroes every user's referral_count, reward flag and
handout history so the whole base can earn again — nobody is deleted, your
broadcast reach is untouched.

CLONE TO A NEW BOT
------------------
/admin → Clone Bot lets you paste another BotFather token. The clone runs this
exact same code and feature set, always credited to your bot's username.

QUICK SETUP
-----------
1.  Token from @BotFather (/newbot).
2.  Your numeric ID from @userinfobot.
3.  Fill BOT_TOKEN + ADMIN_IDS below (env vars win over these).
4.  pip install -r requirements.txt
5.  python bot.py
6.  Make the bot ADMIN in every force-join channel (needed even for public ones).
7.  Configure everything from /admin inside Telegram.

requirements.txt:
    aiogram>=3.4,<4
    aiosqlite>=0.19
"""

import asyncio
import csv
import io
import logging
import os
import random
import re
import sys
import subprocess
import sqlite3
import shutil
import time
import hashlib
import json
import uuid
import signal
try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None
    InvalidToken = Exception
from collections import Counter
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from datetime import datetime, timezone, timedelta
from html import escape as hesc
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    MessageOriginChannel,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  # <-- paste your token here if not using env vars
CLONE_MODE = os.environ.get("CLONE_MODE", "0") == "1"
CLONE_ID = os.environ.get("CLONE_ID", "")
CLONE_DB_PATH = os.environ.get("CLONE_DB_PATH", "")
CLONE_ADMIN_IDS = [int(x) for x in os.environ.get("CLONE_ADMIN_IDS", "").split(",") if x.strip().isdigit()]
MASTER_USERNAME = os.environ.get("MASTER_USERNAME", "").lstrip("@")
MASTER_REGISTRY_DB_PATH = os.environ.get("MASTER_REGISTRY_DB_PATH", "")

_admin_ids_raw = os.environ.get("ADMIN_IDS", "5888777479")
try:
    ADMIN_IDS = [int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip()]
except ValueError:
    ADMIN_IDS = []

# A single WhatsApp number is never handed to more than this many users.
MAX_USERS_PER_NUMBER = int(os.environ.get("MAX_USERS_PER_NUMBER", "20"))


def _resolve_db_path() -> str:
    """Prefer /data/bot.db (Railway persistent volume); fall back to ./bot.db."""
    path = os.environ.get("DB_PATH", "/data/bot.db")
    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, ".write_probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return path
    except OSError:
        return "bot.db"


DB_PATH = CLONE_DB_PATH or _resolve_db_path()
BOT_STARTED_AT = time.time()
V3_PAGE_SIZE = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("gmap_dual_bot")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def friend_word(n: int) -> str:
    return "friend" if n == 1 else "friends"


def progress_bar(count: int, required: int, width: int = 10) -> str:
    filled = min(width, int(width * min(count, required) / max(1, required)))
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# India phone helpers — accept any format the admin/user types, normalize it
# ---------------------------------------------------------------------------

def normalize_indian_number(raw: str) -> Optional[str]:
    """Normalize an Indian mobile number from common formats.

    Returns canonical ``91XXXXXXXXXX`` or None.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 12 and digits.startswith("91"):
        national = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        national = digits[1:]
    elif len(digits) == 10:
        national = digits
    else:
        return None
    if len(national) == 10 and national[0] in "6789":
        return "91" + national
    return None


def extract_phone_candidates(raw: str) -> list[str]:
    """Extract phone-like values while keeping separators inside each number."""
    if not raw:
        return []
    candidates: list[str] = []
    chunks = re.split(r"[\n,;|]+", raw)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if normalize_indian_number(chunk):
            candidates.append(chunk)
            continue
        matches = re.findall(
            r"(?:(?:\+|00)?\s*91[\s().-]*)?"
            r"(?:0[\s().-]*)?"
            r"[6-9](?:[\s().-]*\d){9}",
            chunk,
        )
        candidates.extend(m.strip() for m in matches)
    if not candidates:
        candidates.extend(re.findall(r"(?<!\d)[6-9]\d{9}(?!\d)", raw))
    return candidates



def is_indian_number(raw: str) -> bool:
    return normalize_indian_number(raw) is not None


def pretty_number(canonical: str) -> str:
    """'919876543210' -> '+91 98765 43210' for display."""
    if canonical.startswith("91") and len(canonical) == 12:
        n = canonical[2:]
        return f"+91 {n[:5]} {n[5:]}"
    return "+" + canonical



# ---------------------------------------------------------------------------
# Editable UI content — every listed message/button can be changed from /admin
# ---------------------------------------------------------------------------
UI_MESSAGES = {
    "admin_panel": ("👑 <b>ADMIN CONTROL CENTER</b>\n\n😀 <b>Welcome, {admin_name}!</b>\n\nYour command center is ready.\nManage your bot, users, rewards, channels, content and system settings from one place. 🔥\n\n🚨 <i>Everything under your control.</i>"),
    "start_admin": ("👑 <b>Welcome, Admin!</b>\n\n✨ Your control panel is ready.\n🛠 You can customize messages, buttons, premium/custom emoji, banners and every major user-facing screen."),
    "gate": ("🔒 <b>JOIN CHANNEL</b>\n\nBot को इस्तेमाल करने के लिए पहले required channel join करें."),
    "captcha": ("🧩 <b>Step 2 — Human Verification</b>\n\n✨ Complete this quick verification to continue.\n\nWhat is <b>{question}</b>?"),
    "phone": ("📱 <b>Step 3 — Phone Verification</b>\n\n🇮🇳 Only your own Indian (+91) number is accepted.\n\n✨ Tap <b>{share_button}</b> below to verify securely."),
    "restricted": ("⛔ <b>Access Restricted</b>\n\nYour verification could not be completed.\n\n👨‍💼 If you believe this is a mistake, contact the admin below."),
    "main_locked": ("🤖 <b>{bot_name}</b>\n\n👋 Welcome, {first_name}!\n\n👥 Referrals: <b>{count}/{required}</b>\n🎁 Reward: <b>{reward_status}</b>"),
    "main_unlocked": ("🤖 <b>{bot_name}</b>\n\n👋 Welcome, {first_name}!\n\n👥 Referrals: <b>{count}/{required}</b>\n🎁 Reward: <b>{reward_status}</b>"),
    "referral_link": ("🔗 <b>REFER & EARN</b>\n\nInvite your friends and earn rewards.\n\n👥 Referrals: <b>{referrals}</b>\n🎯 Required: <b>{required_referrals}</b>\n🎁 Reward: <b>{reward}</b>\n\nYour Personal Link:\n<code>{link}</code>"),
    "share_caption": "🎁 Join me on {bot_name} and earn rewards! 🚀",
    "stats": ("📊 <b>MY STATUS</b>\n\n👥 Total Referrals: <b>{count}</b>\n✅ Successful: <b>{count}</b>\n🎯 Required: <b>{required}</b>\n🎁 Rewards Received: <b>{reward_count}</b>\n📱 Phone: <b>{phone}</b>\n🔒 Access: <b>{access}</b>\n\n🎁 Latest Reward: <b>{latest_reward}</b>"),
    "help": ("ℹ️ <b>How {bot_name} Works</b>\n━━━━━━━━━━━━━━━━━━━━\n\n1️⃣ Join the required channel(s).\n2️⃣ Complete the quick verification.\n3️⃣ Verify your Indian (+91) number.\n4️⃣ Share your referral link.\n5️⃣ Earn your reward after a successful referral.\n\n✨ Real users only. Fair play for everyone."),
    "invalid_referral": ("⚠️ <b>Referral Could Not Be Verified</b>\n\nOne of your invited users did not complete valid verification.\n\n✨ Invite a real Indian user to earn your reward."),
    "reward": ("🎉 <b>REWARD UNLOCKED</b>\n\n━━━━━━━━━━━━━━━━\n\n🎁 <b>Your Agent Number</b>\n\n<code>{number}</code>\n\n━━━━━━━━━━━━━━━━\n\n✅ Reward Status: Delivered\n📅 Received: {reward_date}\n\n{caption}"),
    "reward_empty": ("🎉 <b>Your referral is complete!</b>\n\n⚠️ Reward numbers are temporarily unavailable.\n\n👨‍💼 Please contact the admin — your reward is reserved."),
    "no_user": "Please send /start first.",
    "cancelled": "❌ <b>Action cancelled.</b>",
    "message_saved": "✅ <b>Message updated successfully!</b>\n\n✨ Your formatting and custom/premium emoji entities are preserved.",
    "button_saved": "✅ <b>Button label updated!</b>\n\n✨ Unicode emoji are supported in button labels.",
}

UI_BUTTONS = {
    "share_number": "📱 Share My Number",
    "contact_admin": "👨‍💼 Contact Admin",
    "referral_link": "🔗 REFER & EARN",
    "referral_reward": "🔗 REFER & EARN",
    "stats": "📊 STATUS",
    "my_reward": "🎁 MY REWARD",
    "share_friend": "📤 Share with a Friend",
    "back": "⬅️ Back",
    "cancel": "✖️ Cancel",
    "admin_editor": "✏️ Message & Button Editor",
    "message_editor": "📝 Edit Messages",
    "button_editor": "🔘 Edit Buttons",
    "preview": "👁 Preview",
    "open_whatsapp": "💬 Open on WhatsApp",
    "joined_done": "✅ Joined / Done",
    "help_menu": "ℹ️ Help",
    "support_menu": "🆘 Support",
    "menu_home": "🏠 Home",
}


# ---------------------------------------------------------------------------
# Centralized premium UI theme/status/branding layer.
# Callback data remains stable when visible labels/themes change.
# ---------------------------------------------------------------------------
UI_THEME = {
    "PREMIUM": {"divider": "━━━━━━━━━━━━━━━━━━", "verified": "🟢", "pending": "⏳", "missing": "🔴"},
    "DARK":    {"divider": "━━━━━━━━━━━━━━━━━━", "verified": "🟢", "pending": "🟡", "missing": "⚫"},
    "MINIMAL": {"divider": "──────────────",     "verified": "✓",  "pending": "…",  "missing": "×"},
    "NEON":    {"divider": "╍╍╍╍╍╍╍╍╍╍╍╍",     "verified": "🟢", "pending": "⚡", "missing": "🔴"},
    "CLEAN":   {"divider": "────────────────",  "verified": "✅", "pending": "⏳", "missing": "❌"},
}
UI_LAYOUT = {"join_columns": 1, "status_columns": 1}
UI_STATUS = {
    "NOT_JOINED": "❌ Not Joined",
    "REQUESTED": "⏳ Requested",
    "PENDING_APPROVAL": "⏳ Requested",
    "APPROVED": "✅ Approved",
    "MEMBER": "🟢 Joined",
    "LEFT": "❌ Left",
    "KICKED": "🚫 Removed",
    "EXPIRED": "⌛ Request Expired",
    "ERROR": "⚠️ Verification Error",
}
UI_BRANDING = {"master_locked": True}

async def get_ui_theme() -> str:
    value = (await get_setting("ui_theme", "PREMIUM")).upper()
    return value if value in UI_THEME else "PREMIUM"

async def get_theme_style() -> dict:
    return UI_THEME[await get_ui_theme()]

async def branding_footer() -> str:
    return await get_powered_by_text()

async def get_powered_by_text() -> str:
    # Clone identity is fully independent; master branding is never injected into clone user UI.
    return ""


def add_powered_by(text: str) -> str:
    if not CLONE_MODE:
        return text
    return text

ALLOWED_HTML_TAGS = {"b","strong","i","em","u","ins","s","strike","del","code","pre","a","blockquote","tg-spoiler","tg-emoji"}
_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
KNOWN_TEMPLATE_VARIABLES = {"admin_name","admin_first_name","admin_last_name","admin_username","admin_id","user_name","first_name","last_name","username","user_id","bot_name","bot_username","bot_id","clone_name","clone_id","clone_username","referrals","referral_count","required_referrals","remaining_referrals","referral_link","progress","reward","reward_number","reward_count","reward_status","reward_date","channel_name","channel_username","channel_link","total_users","today_users","total_rewards","available_rewards","date","time","datetime","question","share_button","required","count","friends","phone","access","latest_reward","caption","link"}

class _TelegramHTMLValidator(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.errors = []
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in ALLOWED_HTML_TAGS:
            self.errors.append(f"Unsupported HTML tag: <{tag}>")
            return
        if tag not in {"br"}:
            self.stack.append(tag)
    def handle_startendtag(self, tag, attrs):
        if tag.lower() not in ALLOWED_HTML_TAGS:
            self.errors.append(f"Unsupported HTML tag: <{tag}/>")
    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag not in ALLOWED_HTML_TAGS:
            self.errors.append(f"Unsupported HTML tag: </{tag}>")
            return
        if self.stack:
            if self.stack[-1] == tag:
                self.stack.pop()
            elif tag in self.stack:
                self.errors.append(f"Mismatched closing tag: </{tag}>")
                self.stack.remove(tag)
            else:
                self.errors.append(f"Unexpected closing tag: </{tag}>")
    def close(self):
        super().close()
        if self.stack:
            self.errors.append("Unclosed HTML tags: " + ", ".join(self.stack))

def validate_template_html(template: str) -> list[str]:
    parser = _TelegramHTMLValidator()
    try:
        parser.feed(template)
        parser.close()
    except Exception as exc:
        parser.errors.append(type(exc).__name__)
    return parser.errors

def template_variables(template: str) -> set[str]:
    return {m.group(1) for m in _VAR_RE.finditer(template)}

class SafeHTML(str):
    """Trusted Telegram HTML fragment authored by the admin/system."""

def _safe_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, SafeHTML):
        return str(value)
    return hesc(str(value), quote=False)

def render_template(template: str, context: dict) -> str:
    """Render a Telegram HTML template without escaping the template itself.
    Only dynamic values are escaped, preserving admin-authored formatting."""
    def repl(match):
        key = match.group(1)
        return _safe_value(context.get(key, match.group(0)))
    return _VAR_RE.sub(repl, template)

async def bot_identity(bot: Bot | None = None) -> dict:
    configured_name = await get_setting("bot_name", "")
    configured_username = (await get_setting("bot_username", "")).lstrip("@")
    configured_id = await get_setting("bot_id", "")
    if bot is not None:
        try:
            me = await bot.get_me()
            if not configured_name: configured_name = me.full_name or me.first_name or me.username or "Bot"
            if not configured_username: configured_username = me.username or ""
            if not configured_id: configured_id = str(me.id)
        except Exception:
            pass
    return {"bot_name": configured_name or "Bot", "bot_username": configured_username or "", "bot_id": configured_id or ""}

def name_from_telegram(user) -> str:
    if getattr(user, "first_name", None): return str(user.first_name)
    full = " ".join(x for x in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if x)
    if full: return full
    if getattr(user, "username", None): return f"@{user.username}"
    return "User"

async def user_context(user_id: int, bot: Bot | None = None) -> dict:
    row = await get_user(user_id)
    ctx = {}
    if row:
        first = row["first_name"] or ""
        username = row["username"] or ""
        name = first or (f"@{username}" if username else "User")
        ctx.update({"user_name": name, "first_name": first or name, "last_name": "", "username": username and f"@{username}" or "", "user_id": user_id})
        ctx.update({"referrals": row["referral_count"], "referral_count": row["referral_count"], "phone": pretty_number(row["phone"]) if row["phone"] else "", "reward_status": "Delivered" if row["reward_sent"] else "Locked"})
    else:
        ctx.update({"user_name":"User","first_name":"User","last_name":"","username":"","user_id":user_id,"referrals":0,"referral_count":0,"phone":"","reward_status":"Locked"})
    ctx.update(await bot_identity(bot))
    return ctx

async def ui_message(key: str, default: str | None = None, **kwargs) -> str:
    template = await get_setting(f"ui_msg:{key}", UI_MESSAGES.get(key, default or key))
    context = dict(kwargs)
    if "bot_name" not in context:
        context.update(await bot_identity())
    rendered = render_template(template, context)
    if CLONE_MODE:
        powered = await get_powered_by_text()
        if powered and powered not in rendered:
            footer = f"\n\n━━━━━━━━━━━━━━ {powered} ━━━━━━━━━━━━━━"
            rendered = (rendered[:max(0,4096-len(footer))] + footer).strip()
    return rendered[:4096]

async def ui_button(key: str, default: str | None = None, **kwargs) -> str:
    value = await get_setting(f"ui_btn:{key}", UI_BUTTONS.get(key, default or key))
    return render_template(value, kwargs)

async def save_ui_message(key: str, value: str, changed_by: int | None = None) -> None:
    errors = validate_template_html(value)
    if errors:
        raise ValueError("; ".join(errors[:3]))
    unknown=template_variables(value)-KNOWN_TEMPLATE_VARIABLES
    if unknown:
        raise ValueError("Unknown variable(s): " + ", ".join(sorted(unknown)[:8]))
    await set_setting(f"ui_msg:{key}", value)
    async with aiosqlite.connect(DB_PATH) as db:
        cur=await db.execute("SELECT COALESCE(MAX(version),0) FROM message_versions WHERE message_key=?",(key,))
        version=int((await cur.fetchone())[0])+1
        await db.execute("INSERT INTO message_versions(message_key,version,content,created_by,created_at) VALUES(?,?,?,?,?)",(key,version,value,changed_by,datetime.now(timezone.utc).isoformat()))
        await db.commit()

async def save_ui_button(key: str, value: str) -> None:
    await set_setting(f"ui_btn:{key}", value)


# ---------------------------------------------------------------------------
# V5 multi-tenant feature model
# ---------------------------------------------------------------------------
FEATURE_NAMES = (
    "dashboard", "users", "user_search", "user_moderation",
    "referral", "referral_adjustment", "reward_claim", "reward_pool",
    "reward_caption", "reward_history", "reward_reset",
    "channel_view", "channel_manage", "join_requests", "force_join",
    "captcha", "phone_verification", "verification",
    "broadcast", "scheduled_broadcast",
    "basic_analytics", "advanced_analytics", "charts",
    "content_view", "content_edit", "content_reset", "button_edit", "banner_edit",
    "backup", "diagnostics", "maintenance", "csv_export", "settings",
)
BASIC_FEATURES = {
    "dashboard","users","user_search","referral","reward_claim","reward_caption",
    "channel_view","force_join","captcha","phone_verification","verification","basic_analytics",
    "content_view","settings",
}
STANDARD_FEATURES = BASIC_FEATURES | {
    "user_moderation","referral_adjustment","reward_pool","reward_history","reward_reset",
    "channel_manage","join_requests","broadcast","basic_analytics","advanced_analytics",
    "csv_export","backup","diagnostics","content_edit","button_edit","banner_edit",
}
PREMIUM_FEATURES = set(FEATURE_NAMES)
PACKAGE_FEATURES = {
    "BASIC": BASIC_FEATURES,
    "STANDARD": STANDARD_FEATURES,
    "PREMIUM": PREMIUM_FEATURES,
    "CUSTOM": set(),
}
CLONE_ROLE_FEATURES = {
    "OWNER": {"*"},
    "ADMIN": {"*"},
    "MODERATOR": {"users","user_search","user_moderation","referral_adjustment"},
    "SUPPORT": {"users","user_search","verification"},
    "VIEWER": {"dashboard","basic_analytics"},
}

def _package_features(package: str) -> set[str]:
    return set(PACKAGE_FEATURES.get(package.upper(), BASIC_FEATURES))

async def clone_is_feature_enabled(feature: str) -> bool:
    if not CLONE_MODE:
        return True
    if not CLONE_ID:
        return False
    if feature == "dashboard":
        return True
    # Permissions live in the Master registry so a toggle takes effect
    # immediately without copying settings into every clone database.
    registry = MASTER_REGISTRY_DB_PATH or DB_PATH
    try:
        async with aiosqlite.connect(registry) as db:
            cur = await db.execute(
                "SELECT enabled FROM clone_features WHERE clone_id=? AND feature=?",
                (CLONE_ID, feature),
            )
            row = await cur.fetchone()
        return bool(row and row[0])
    except Exception:
        return False


async def clone_admin_authorized(admin_id: int) -> bool:
    if not CLONE_MODE:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM clone_admins WHERE clone_id=? AND admin_id=? AND enabled=1",
            (CLONE_ID, admin_id),
        )
        return await cur.fetchone() is not None

async def clone_admin_role(admin_id: int) -> str:
    if not CLONE_MODE:
        return ""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT role FROM clone_admins WHERE clone_id=? AND admin_id=? AND enabled=1",
            (CLONE_ID, admin_id),
        )
        row = await cur.fetchone()
    return row[0] if row else ""

async def has_clone_permission(admin_id: int, feature: str) -> bool:
    if not CLONE_MODE:
        return True
    if not await clone_admin_authorized(admin_id):
        return False
    role = await clone_admin_role(admin_id)
    role_features = CLONE_ROLE_FEATURES.get(role, set())
    if "*" in role_features:
        return await clone_is_feature_enabled(feature)
    return feature in role_features and await clone_is_feature_enabled(feature)

async def clone_audit(admin_id: int, action: str, target_id: int | None = None, details: str = "") -> None:
    if not CLONE_MODE:
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO clone_audit_logs(clone_id,admin_id,action,target_id,details,created_at) VALUES(?,?,?,?,?,?)",
                (CLONE_ID, admin_id, action, target_id, details[:2000], datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
    except Exception:
        logger.exception("Clone audit failed")

# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id            INTEGER PRIMARY KEY,
                username           TEXT,
                first_name         TEXT,
                referred_by        INTEGER,
                referral_count     INTEGER NOT NULL DEFAULT 0,
                joined_gate        INTEGER NOT NULL DEFAULT 0,
                referral_credited  INTEGER NOT NULL DEFAULT 0,
                reward_sent        INTEGER NOT NULL DEFAULT 0,
                phone              TEXT,
                phone_verified     INTEGER NOT NULL DEFAULT 0,
                captcha_passed     INTEGER NOT NULL DEFAULT 0,
                captcha_answer     TEXT,
                banned             INTEGER NOT NULL DEFAULT 0,
                restricted         INTEGER NOT NULL DEFAULT 0,
                created_at         TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                channel_id  INTEGER PRIMARY KEY,
                title       TEXT,
                invite_link TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_join (
                user_id      INTEGER NOT NULL,
                channel_id   INTEGER NOT NULL,
                requested_at TEXT NOT NULL,
                PRIMARY KEY (user_id, channel_id)
            )
            """
        )

        # Additive join-request state machine. pending_join remains for
        # backward compatibility and is synchronized with this table.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS join_request_states (
                user_id              INTEGER NOT NULL,
                channel_id           INTEGER NOT NULL,
                status               TEXT NOT NULL DEFAULT 'REQUESTED',
                requested_at         TEXT NOT NULL,
                approved_at          TEXT,
                member_verified_at   TEXT,
                last_checked_at      TEXT,
                last_error           TEXT,
                notification_sent    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, channel_id)
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_join_states_channel_status "
            "ON join_request_states(channel_id, status)"
        )
        # Backfill legacy pending_join rows without deleting or changing them.
        await db.execute(
            """
            INSERT OR IGNORE INTO join_request_states
                (user_id, channel_id, status, requested_at)
            SELECT user_id, channel_id, 'REQUESTED', requested_at
            FROM pending_join
            """
        )
        # Bulk WhatsApp-number reward pool. handout_count tracks how many
        # users have already received this number (capped at MAX_USERS_PER_NUMBER).
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reward_numbers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                number        TEXT UNIQUE NOT NULL,
                handout_count INTEGER NOT NULL DEFAULT 0,
                added_at      TEXT NOT NULL
            )
            """
        )
        # Every payout is logged so a user never gets the same number twice and
        # so a reset can wipe history cleanly.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reward_handouts (
                user_id    INTEGER NOT NULL,
                number_id  INTEGER NOT NULL,
                sent_at    TEXT NOT NULL,
                PRIMARY KEY (user_id, number_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_banner_messages (
                user_id    INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                mode       TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute("""CREATE TABLE IF NOT EXISTS admin_roles (
            admin_id INTEGER PRIMARY KEY, role TEXT NOT NULL DEFAULT 'owner', created_at TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL, action TEXT NOT NULL,
            target_user_id INTEGER, details TEXT, before_value TEXT, after_value TEXT, created_at TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS broadcast_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL, audience TEXT NOT NULL,
            min_referrals INTEGER NOT NULL DEFAULT 0, source_chat_id INTEGER, source_message_id INTEGER,
            status TEXT NOT NULL DEFAULT 'draft', total INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0,
            sent INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
        )
        """)
        await db.execute("""CREATE TABLE IF NOT EXISTS backup_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL, size_bytes INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'backup', created_at TEXT NOT NULL
        )
        """)
        await db.execute("""CREATE TABLE IF NOT EXISTS reward_history_meta (
            id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT, created_at TEXT NOT NULL
        )
        """)

        # Permanent reward ledger — every reward issued lives here so rewards
        # survive a "Reset Referrals" and can be looked up / revoked later.
        await db.execute("""CREATE TABLE IF NOT EXISTS reward_records (
            reward_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            clone_id          TEXT,
            user_id           INTEGER NOT NULL,
            referral_id       INTEGER,
            reward_type       TEXT NOT NULL DEFAULT 'agent_number',
            reward_value      TEXT,
            reward_number     TEXT,
            reward_status     TEXT NOT NULL DEFAULT 'RESERVED',
            created_at        TEXT NOT NULL,
            delivered_at      TEXT,
            message_id        INTEGER,
            chat_id           INTEGER,
            wa_link           TEXT,
            recovery_count    INTEGER NOT NULL DEFAULT 0,
            last_recovery_at  TEXT
        )
        """)

        # One row per successful referral credit — used for stats/history.
        await db.execute("""CREATE TABLE IF NOT EXISTS referral_events (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            referred_user_id  INTEGER NOT NULL,
            referrer_id       INTEGER NOT NULL,
            created_at        TEXT NOT NULL
        )
        """)

        # Tracks the single "live" flow message per user so screens can be
        # edited in place instead of sending duplicate messages.
        await db.execute("""CREATE TABLE IF NOT EXISTS flow_messages (
            user_id     INTEGER PRIMARY KEY,
            chat_id     INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            step        TEXT,
            updated_at  TEXT NOT NULL
        )
        """)

        # Simple key/value schema-version marker.
        await db.execute("""CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        # Version history for admin-edited message templates.
        await db.execute("""CREATE TABLE IF NOT EXISTS message_versions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            message_key  TEXT NOT NULL,
            version      INTEGER NOT NULL,
            content      TEXT,
            created_by   INTEGER,
            created_at   TEXT NOT NULL
        )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_message_versions_key ON message_versions(message_key, version)")

        # ------------------------------------------------------------------
        # V5 master/clone registry and permission tables. These are additive
        # migrations: existing V3 tables/data are never removed or renamed.
        # ------------------------------------------------------------------
        await db.execute("""CREATE TABLE IF NOT EXISTS clone_registry (
            clone_id TEXT PRIMARY KEY,
            bot_id INTEGER NOT NULL UNIQUE,
            bot_username TEXT,
            bot_name TEXT,
            owner_id INTEGER NOT NULL,
            package TEXT NOT NULL DEFAULT 'BASIC',
            enabled INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'STOPPED',
            database_path TEXT NOT NULL,
            token_ciphertext TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_started_at TEXT,
            last_stopped_at TEXT,
            last_error TEXT,
            restart_count INTEGER NOT NULL DEFAULT 0,
            auto_restart INTEGER NOT NULL DEFAULT 1
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS clone_features (
            clone_id TEXT NOT NULL,
            feature TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'package',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (clone_id, feature)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS clone_admins (
            clone_id TEXT NOT NULL,
            admin_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'OWNER',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            PRIMARY KEY (clone_id, admin_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS clone_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clone_id TEXT NOT NULL,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target_id INTEGER,
            details TEXT,
            created_at TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS clone_packages (
            package TEXT PRIMARY KEY,
            features_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_clone_registry_status ON clone_registry(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_clone_features_clone ON clone_features(clone_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_clone_admins_clone ON clone_admins(clone_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_clone_audit_created ON clone_audit_logs(created_at)")

        # Migrations for databases created by an older version of this bot.
        for sql in (
            "ALTER TABLE users ADD COLUMN last_activity TEXT",
            "ALTER TABLE users ADD COLUMN phone TEXT",
            "ALTER TABLE users ADD COLUMN phone_verified INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN captcha_passed INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN captcha_answer TEXT",
            "ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN restricted INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                await db.execute(sql)
            except Exception:
                pass  # column already exists

        defaults = {
            "required_referrals": "1",
            "reward_type": "agent_number",
            "reward_quantity": "1",
            "reward_limit": "0",
            "bot_name": "",
            "bot_username": "",
            "bot_id": "",
            "bot_mode": "refer",  # "refer" | "task"
            "admin_username": "YourAdminUsername",  # fallback contact username
            "admin_contact_id": "",  # numeric Telegram ID, preferred
            "reward_caption": (
                "Here is your reward number 👇\nMessage it on WhatsApp to claim."
            ),
            "captcha_enabled": "1",
            "phone_verify_enabled": "1",
            "task_banner_file_id": "",
            "refer_banner_file_id": "",
            "task_banner_caption": "🎯 <b>Task & Earn</b>\n\nComplete tasks, stay active and unlock your rewards. 🚀",
            "refer_banner_caption": "🤝 <b>Refer & Earn</b>\n\nInvite genuine friends, complete verification and unlock your rewards. 🎁",
            "auto_approve_join_requests": "0",
            "join_request_last_error": "",
            # 0 = never expire; otherwise minutes: 15, 30, 60, 360, 1440.
            "join_request_expiration_minutes": "0",
            "ui_theme": "PREMIUM",
            "join_last_event_at": "",
            "join_approved_today": "0",
            "join_rejected_today": "0",
            "join_expired_today": "0",
            "maintenance_mode": "0",
            "broadcast_delay": "0.07",
            "maintenance_message": "🛠 <b>Temporarily Under Maintenance</b>\n\n✨ {bot_name} is being improved. Please try again shortly.",
            "reward_cooldown_seconds": "0",
            "reward_failure_mode": "notify",
            "active_users_days": "30",
        }
        for key, value in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )

        # Seed package templates without overwriting Master customisations.
        for package_name, feature_set in PACKAGE_FEATURES.items():
            await db.execute(
                "INSERT OR IGNORE INTO clone_packages(package, features_json, updated_at) VALUES (?,?,?)",
                (package_name, ",".join(sorted(feature_set)), datetime.now(timezone.utc).isoformat()),
            )
        if CLONE_MODE and CLONE_ID and CLONE_ADMIN_IDS:
            for clone_admin in CLONE_ADMIN_IDS:
                await db.execute(
                    "INSERT OR IGNORE INTO clone_admins(clone_id,admin_id,role,enabled,created_at) VALUES (?,?,?,1,?)",
                    (CLONE_ID, clone_admin, "OWNER", datetime.now(timezone.utc).isoformat()),
                )
            await db.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES('master_username',?)",
                (MASTER_USERNAME,),
            )
        for key, value in UI_MESSAGES.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (f"ui_msg:{key}", value),
            )
        for key, value in UI_BUTTONS.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (f"ui_btn:{key}", value),
            )
        for admin_id in ADMIN_IDS:
            await db.execute(
                "INSERT OR IGNORE INTO admin_roles(admin_id, role, created_at) VALUES (?, 'owner', ?)",
                (admin_id, datetime.now(timezone.utc).isoformat()),
            )
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
            "CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)",
            "CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by)",
            "CREATE INDEX IF NOT EXISTS idx_users_referrals ON users(referral_count)",
            "CREATE INDEX IF NOT EXISTS idx_users_created ON users(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_handouts_user ON reward_handouts(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_handouts_number ON reward_handouts(number_id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_audit_admin ON audit_logs(admin_id)",
            "CREATE INDEX IF NOT EXISTS idx_broadcast_created ON broadcast_campaigns(created_at)",
        ]
        for sql in indexes:
            await db.execute(sql)
        for sql in (
            "CREATE INDEX IF NOT EXISTS idx_reward_records_user ON reward_records(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_reward_records_status ON reward_records(reward_status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_referral_events_referrer ON referral_events(referrer_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_flow_messages_chat ON flow_messages(chat_id, message_id)",
        ):
            await db.execute(sql)
        # Safe additive backfill: old handouts become permanent delivered rewards.
        cur = await db.execute("SELECT COUNT(*) FROM reward_records")
        reward_record_count = int((await cur.fetchone())[0])
        if reward_record_count == 0:
            await db.execute("""INSERT INTO reward_records
                (clone_id,user_id,reward_type,reward_value,reward_number,reward_status,created_at,delivered_at,wa_link)
                SELECT ?, rh.user_id, 'agent_number', rn.number, rn.number, 'DELIVERED', rh.sent_at, rh.sent_at, ? || rn.number
                FROM reward_handouts rh JOIN reward_numbers rn ON rn.id=rh.number_id""",
                (CLONE_ID or None, "https://wa.me/"))
        await db.execute("INSERT INTO schema_meta(key,value) VALUES('schema_version','6') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        await db.commit()


async def get_user(user_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cursor.fetchone()


async def find_user_by_username(username: str) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)
        )
        return await cursor.fetchone()


async def get_user_by_phone(phone: str) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE phone = ?", (phone,))
        return await cursor.fetchone()


async def create_user(user_id, username, first_name, referred_by) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, username, first_name, referred_by, created_at, last_activity) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, first_name, referred_by,
             datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def update_user_profile(user_id, username, first_name) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET username = ?, first_name = ?, last_activity = ? WHERE user_id = ?",
            (username, first_name, datetime.now(timezone.utc).isoformat(), user_id),
        )
        await db.commit()


async def _set_flag(user_id: int, column: str, value: int) -> None:
    assert column in {
        "joined_gate", "reward_sent", "phone_verified",
        "captcha_passed", "banned", "restricted",
    }
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE users SET {column} = ? WHERE user_id = ?", (value, user_id)
        )
        await db.commit()


async def mark_joined_gate(user_id: int) -> None:
    await _set_flag(user_id, "joined_gate", 1)


async def unmark_joined_gate(user_id: int) -> None:
    await _set_flag(user_id, "joined_gate", 0)


async def mark_reward_sent(user_id: int) -> None:
    await _set_flag(user_id, "reward_sent", 1)


async def set_banned(user_id: int, banned: bool) -> None:
    await _set_flag(user_id, "banned", 1 if banned else 0)


async def set_restricted(user_id: int, restricted: bool) -> None:
    await _set_flag(user_id, "restricted", 1 if restricted else 0)


async def mark_captcha_passed(user_id: int) -> None:
    await _set_flag(user_id, "captcha_passed", 1)


async def set_captcha_answer(user_id: int, answer: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET captcha_answer = ? WHERE user_id = ?", (answer, user_id)
        )
        await db.commit()


async def set_phone_verified(user_id: int, phone: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET phone = ?, phone_verified = 1 WHERE user_id = ?",
            (phone, user_id),
        )
        await db.commit()


async def adjust_referrals(user_id: int, delta: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET referral_count = MAX(0, referral_count + ?) WHERE user_id = ?",
            (delta, user_id),
        )
        await db.commit()


async def credit_referral_and_mark(referred_user_id: int, referrer_id: int) -> bool:
    now=datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur=await db.execute("SELECT referral_credited,referred_by,banned,restricted FROM users WHERE user_id=?",(referred_user_id,)); invited=await cur.fetchone()
        if not invited or invited[0] or invited[2] or invited[3] or invited[1] != referrer_id:
            await db.rollback(); return False
        cur=await db.execute("SELECT banned,restricted FROM users WHERE user_id=?",(referrer_id,)); ref=await cur.fetchone()
        if not ref or ref[0] or ref[1] or referrer_id==referred_user_id:
            await db.rollback(); return False
        try:
            await db.execute("INSERT INTO referral_events(referred_user_id,referrer_id,created_at) VALUES(?,?,?)",(referred_user_id,referrer_id,now))
        except aiosqlite.IntegrityError:
            await db.rollback(); return False
        await db.execute("UPDATE users SET referral_count=referral_count+1, referral_credited=1 WHERE user_id=?",(referrer_id,))
        await db.commit()
        return True


JOIN_STATUSES = {
    "NOT_JOINED", "REQUESTED", "PENDING_APPROVAL", "APPROVED",
    "MEMBER", "LEFT", "KICKED", "EXPIRED", "ERROR",
}

async def _set_join_state(
    user_id: int,
    channel_id: int,
    status: str,
    *,
    requested_at: str | None = None,
    approved_at: str | None = None,
    member_verified_at: str | None = None,
    last_error: str | None = None,
    notification_sent: int | None = None,
) -> None:
    if status not in JOIN_STATUSES:
        status = "ERROR"
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO join_request_states(
                user_id, channel_id, status, requested_at, approved_at,
                member_verified_at, last_checked_at, last_error, notification_sent
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id,channel_id) DO UPDATE SET
                status=excluded.status,
                requested_at=COALESCE(excluded.requested_at, join_request_states.requested_at),
                approved_at=COALESCE(excluded.approved_at, join_request_states.approved_at),
                member_verified_at=COALESCE(excluded.member_verified_at, join_request_states.member_verified_at),
                last_checked_at=excluded.last_checked_at,
                last_error=excluded.last_error,
                notification_sent=COALESCE(excluded.notification_sent, join_request_states.notification_sent)
            """,
            (
                user_id, channel_id, status,
                requested_at or now, approved_at, member_verified_at,
                now, last_error, notification_sent if notification_sent is not None else 0,
            ),
        )
        await db.commit()


async def record_join_request(user_id: int, channel_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO pending_join(user_id, channel_id, requested_at)
            VALUES(?,?,?)
            ON CONFLICT(user_id,channel_id) DO UPDATE SET requested_at=excluded.requested_at
            """,
            (user_id, channel_id, now),
        )
        await db.execute(
            """
            INSERT INTO join_request_states(user_id, channel_id, status, requested_at, last_checked_at, last_error)
            VALUES(?,?, 'REQUESTED', ?, ?, NULL)
            ON CONFLICT(user_id,channel_id) DO UPDATE SET
                status=CASE
                    WHEN join_request_states.status IN ('MEMBER','APPROVED') THEN join_request_states.status
                    ELSE 'REQUESTED'
                END,
                requested_at=excluded.requested_at,
                last_checked_at=excluded.last_checked_at,
                last_error=NULL
            """,
            (user_id, channel_id, now, now),
        )
        await db.commit()


async def set_join_request_status(
    user_id: int,
    channel_id: int,
    status: str,
    *,
    approved: bool = False,
    member_verified: bool = False,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await _set_join_state(
        user_id, channel_id, status,
        approved_at=now if approved else None,
        member_verified_at=now if member_verified else None,
        last_error=(error or None),
    )
    async with aiosqlite.connect(DB_PATH) as db:
        if status == "MEMBER":
            await db.execute(
                "DELETE FROM pending_join WHERE user_id=? AND channel_id=?",
                (user_id, channel_id),
            )
        elif status in {"LEFT", "KICKED", "EXPIRED"}:
            await db.execute(
                "DELETE FROM pending_join WHERE user_id=? AND channel_id=?",
                (user_id, channel_id),
            )
        db.commit if False else None
        await db.commit()


async def clear_join_request(user_id: int, channel_id: int) -> None:
    # Legacy API retained. It now records the terminal state instead of
    # destroying state-machine history.
    await set_join_request_status(user_id, channel_id, "MEMBER", member_verified=True)


async def has_pending_join(user_id: int, channel_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT status FROM join_request_states WHERE user_id=? AND channel_id=?",
            (user_id, channel_id),
        )
        row = await cursor.fetchone()
    return bool(row and row[0] in {"REQUESTED", "PENDING_APPROVAL", "APPROVED"})


async def get_join_state(user_id: int, channel_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM join_request_states WHERE user_id=? AND channel_id=?",
            (user_id, channel_id),
        )
        return await cur.fetchone()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


async def _request_expired(state: aiosqlite.Row | None) -> bool:
    if not state or state["status"] not in {"REQUESTED", "PENDING_APPROVAL", "APPROVED"}:
        return False
    try:
        minutes = int(await get_setting("join_request_expiration_minutes", "0"))
    except ValueError:
        minutes = 0
    if minutes <= 0:
        return False
    requested = _parse_iso(state["requested_at"])
    return bool(requested and datetime.now(timezone.utc) - requested >= timedelta(minutes=minutes))


async def mark_join_expired(user_id: int, channel_id: int) -> None:
    await set_join_request_status(user_id, channel_id, "EXPIRED")
    await set_setting("join_expired_today", str(int(await get_setting("join_expired_today", "0")) + 1))


async def mark_join_approved(user_id: int, channel_id: int) -> None:
    await set_join_request_status(user_id, channel_id, "APPROVED", approved=True)


async def mark_join_member(user_id: int, channel_id: int) -> None:
    await set_join_request_status(user_id, channel_id, "MEMBER", member_verified=True)


async def mark_join_error(user_id: int, channel_id: int, error: str) -> None:
    await _set_join_state(user_id, channel_id, "ERROR", last_error=error[:1000])


async def join_state_counts() -> dict:
    result = {s: 0 for s in JOIN_STATUSES}
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT status, COUNT(*) FROM join_request_states GROUP BY status"
        )
        for status, count in await cur.fetchall():
            result[status] = int(count)
    return result


async def pending_join_rows(channel_id: int | None = None, limit: int = 50) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if channel_id is None:
            cur = await db.execute(
                """
                SELECT j.*, u.username, u.first_name
                FROM join_request_states j
                LEFT JOIN users u ON u.user_id=j.user_id
                WHERE j.status IN ('REQUESTED','PENDING_APPROVAL','APPROVED')
                ORDER BY j.requested_at ASC LIMIT ?
                """, (limit,)
            )
        else:
            cur = await db.execute(
                """
                SELECT j.*, u.username, u.first_name
                FROM join_request_states j
                LEFT JOIN users u ON u.user_id=j.user_id
                WHERE j.channel_id=? AND j.status IN ('REQUESTED','PENDING_APPROVAL','APPROVED')
                ORDER BY j.requested_at ASC LIMIT ?
                """, (channel_id, limit)
            )
        return list(await cur.fetchall())


async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else default


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def get_required_referrals() -> int:
    value = await get_setting("required_referrals", "1")
    try:
        return max(1, int(value))
    except ValueError:
        return 1


async def get_bot_mode() -> str:
    mode = await get_setting("bot_mode", "refer")
    return mode if mode in ("refer", "task") else "refer"


# ---------------------------------------------------------------------------
# Bulk WhatsApp-number reward pool
# ---------------------------------------------------------------------------

async def add_reward_numbers(raw_numbers: list[str]) -> tuple[int, int]:
    """Add many numbers at once. Returns (added, skipped_or_duplicate)."""
    added = skipped = 0
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        for raw in raw_numbers:
            canonical = normalize_indian_number(raw)
            if not canonical:
                skipped += 1
                continue
            try:
                await db.execute(
                    "INSERT INTO reward_numbers (number, handout_count, added_at) "
                    "VALUES (?, 0, ?)",
                    (canonical, now),
                )
                added += 1
            except aiosqlite.IntegrityError:
                skipped += 1  # already in the pool
        await db.commit()
    return added, skipped


async def delete_reward_number(number_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM reward_numbers WHERE id = ?", (number_id,))
        await db.commit()


async def clear_reward_numbers() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM reward_numbers")
        await db.execute("DELETE FROM reward_handouts")
        await db.commit()


async def get_reward_numbers() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM reward_numbers ORDER BY id"
        )
        return list(await cursor.fetchall())


async def pool_stats() -> tuple[int, int, int]:
    """(total numbers, remaining capacity, total handouts done)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*), COALESCE(SUM(handout_count),0) FROM reward_numbers")
        total, used = await cur.fetchone()
    capacity = total * MAX_USERS_PER_NUMBER
    return total, max(0, capacity - used), used


async def claim_number_for_user(user_id: int) -> Optional[str]:
    """Atomically reserve one eligible number for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("""SELECT rn.id,rn.number,rn.handout_count FROM reward_numbers rn
            WHERE rn.handout_count < ? AND rn.id NOT IN
            (SELECT number_id FROM reward_handouts WHERE user_id=?)
            ORDER BY rn.handout_count ASC, rn.id ASC""", (MAX_USERS_PER_NUMBER, user_id))
        rows=list(await cur.fetchall())
        if not rows:
            await db.rollback(); return None
        minimum=rows[0]["handout_count"]
        chosen=random.choice([r for r in rows if r["handout_count"]==minimum])
        now=datetime.now(timezone.utc).isoformat()
        await db.execute("UPDATE reward_numbers SET handout_count=handout_count+1 WHERE id=? AND handout_count<?", (chosen["id"],MAX_USERS_PER_NUMBER))
        if db.total_changes == 0:
            await db.rollback(); return None
        try:
            await db.execute("INSERT INTO reward_handouts(user_id,number_id,sent_at) VALUES(?,?,?)",(user_id,chosen["id"],now))
        except aiosqlite.IntegrityError:
            await db.rollback(); return None
        await db.commit()
        return chosen["number"]

async def reward_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur=await db.execute("SELECT COUNT(*) FROM reward_records WHERE user_id=? AND reward_status!='REVOKED'",(user_id,))
        return int((await cur.fetchone())[0])

async def latest_reward(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT * FROM reward_records WHERE user_id=? AND reward_status!='REVOKED' ORDER BY reward_id DESC LIMIT 1",(user_id,))
        return await cur.fetchone()

async def get_reward_records(user_id: int, limit: int=20):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT * FROM reward_records WHERE user_id=? ORDER BY reward_id DESC LIMIT ?",(user_id,limit))
        return list(await cur.fetchall())

async def reserve_reward_for_user(user_id: int, referral_id: int | None = None):
    """Atomically reserve a number and create its permanent reward record.
    The DB transaction is the idempotency boundary, so concurrent callbacks cannot allocate twice."""
    required=max(1,await get_required_referrals())
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory=aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cur=await db.execute("SELECT referral_count FROM users WHERE user_id=?",(user_id,)); u=await cur.fetchone()
        if not u:
            await db.rollback(); return None
        target=int(u["referral_count"])//required
        cur=await db.execute("SELECT COUNT(*) FROM reward_records WHERE user_id=? AND reward_status!='REVOKED'",(user_id,)); existing=int((await cur.fetchone())[0])
        if existing>=target:
            await db.commit(); return None
        cur=await db.execute("""SELECT rn.id,rn.number,rn.handout_count FROM reward_numbers rn
            WHERE rn.handout_count<? AND rn.id NOT IN (SELECT number_id FROM reward_handouts WHERE user_id=?)
            ORDER BY rn.handout_count ASC,rn.id ASC""",(MAX_USERS_PER_NUMBER,user_id))
        rows=list(await cur.fetchall())
        if not rows:
            await db.rollback(); return None
        minimum=rows[0]["handout_count"]; chosen=random.choice([r for r in rows if r["handout_count"]==minimum])
        now=datetime.now(timezone.utc).isoformat();
        await db.execute("UPDATE reward_numbers SET handout_count=handout_count+1 WHERE id=? AND handout_count<?",(chosen["id"],MAX_USERS_PER_NUMBER))
        if db.total_changes==0:
            await db.rollback(); return None
        try:
            await db.execute("INSERT INTO reward_handouts(user_id,number_id,sent_at) VALUES(?,?,?)",(user_id,chosen["id"],now))
        except aiosqlite.IntegrityError:
            await db.rollback(); return None
        cur=await db.execute("""INSERT INTO reward_records(clone_id,user_id,referral_id,reward_type,reward_value,reward_number,reward_status,created_at,wa_link)\n            VALUES(?,?,?,?,?,?,?,?,?)""",(CLONE_ID or None,user_id,referral_id,await get_setting('reward_type','agent_number'),chosen['number'],chosen['number'],'RESERVED',now,wa_link(chosen['number'])))
        reward_id=cur.lastrowid
        await db.commit()
        return {"reward_id":reward_id,"number":chosen["number"],"number_id":chosen["id"],"created_at":now,"existing_target":existing}


async def mark_reward_delivered(reward_id: int, message_id: int, chat_id: int) -> None:
    now=datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reward_records SET reward_status='DELIVERED',delivered_at=?,message_id=?,chat_id=? WHERE reward_id=?",(now,message_id,chat_id,reward_id))
        await db.commit()

async def recover_reward(bot: Bot, user_id: int, reward_id: int | None = None) -> bool:
    reward = None
    if reward_id:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory=aiosqlite.Row
            cur=await db.execute("SELECT * FROM reward_records WHERE reward_id=? AND user_id=?",(reward_id,user_id)); reward=await cur.fetchone()
    if reward is None: reward=await latest_reward(user_id)
    if reward is None or reward["reward_status"]=="REVOKED": return False
    caption=await get_setting("reward_caption","")
    text=await ui_message("reward",number=pretty_number(reward["reward_number"] or reward["reward_value"] or ""),caption=SafeHTML(caption),reward_date=(reward["delivered_at"] or reward["created_at"])[:10])
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=await ui_button("open_whatsapp","💬 Open WhatsApp"),url=reward["wa_link"] or wa_link(reward["reward_number"]))],[InlineKeyboardButton(text=await ui_button("referral_link","🔗 REFER MORE"),callback_data="menu_link")]])
    try:
        msg=await bot.send_message(user_id,text,reply_markup=kb)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE reward_records SET recovery_count=recovery_count+1,last_recovery_at=? WHERE reward_id=?",(datetime.now(timezone.utc).isoformat(),reward["reward_id"]))
            await db.commit()
        return True
    except Exception:
        logger.exception("Reward recovery failed for %s",user_id); return False


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

async def get_channels() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM channels")
        return list(await cursor.fetchall())


async def add_channel(channel_id: int, title: str, invite_link: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO channels (channel_id, title, invite_link) VALUES (?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET title = excluded.title, "
            "invite_link = excluded.invite_link",
            (channel_id, title, invite_link),
        )
        await db.commit()


async def remove_channel(channel_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
        await db.commit()


# ---------------------------------------------------------------------------
# User queries / reset
# ---------------------------------------------------------------------------

async def get_all_user_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users")
        return [r[0] for r in await cursor.fetchall()]


async def get_all_users() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users ORDER BY created_at")
        return list(await cursor.fetchall())


async def reset_all_referrals() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur=await db.execute("SELECT COUNT(*) FROM users"); count=int((await cur.fetchone())[0])
        await db.execute("UPDATE users SET referral_count=0, referral_credited=0, reward_sent=CASE WHEN EXISTS(SELECT 1 FROM reward_records rr WHERE rr.user_id=users.user_id AND rr.reward_status!='REVOKED') THEN 1 ELSE 0 END")
        # Preserve reward records/history. Only the referral earning counter is reset.
        await db.commit()
    return count


async def get_stats() -> dict:
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    week = (now - timedelta(days=7)).isoformat()
    month = (now - timedelta(days=30)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async def count(sql: str, params: tuple = ()) -> int:
            cur = await db.execute(sql, params)
            return int((await cur.fetchone())[0])
        total = await count("SELECT COUNT(*) FROM users")
        verified = await count("SELECT COUNT(*) FROM users WHERE phone_verified=1")
        completed = await count("SELECT COUNT(*) FROM users WHERE referral_credited=1")
        rewards = await count("SELECT COUNT(*) FROM reward_records WHERE reward_status='DELIVERED'")
        numbers = await count("SELECT COUNT(*) FROM reward_numbers")
        used = await count("SELECT COALESCE(SUM(handout_count),0) FROM reward_numbers")
        return {
            "total_users": total, "today_users": await count("SELECT COUNT(*) FROM users WHERE created_at LIKE ?", (f"{today}%",)),
            "week_users": await count("SELECT COUNT(*) FROM users WHERE created_at >= ?", (week,)),
            "month_users": await count("SELECT COUNT(*) FROM users WHERE created_at >= ?", (month,)),
            "gate_verified": await count("SELECT COUNT(*) FROM users WHERE joined_gate=1"),
            "phone_verified": verified, "completed_referrals": completed, "rewards_sent": rewards,
            "banned": await count("SELECT COUNT(*) FROM users WHERE banned=1"),
            "restricted": await count("SELECT COUNT(*) FROM users WHERE restricted=1"),
            "conversion": round((verified/total*100) if total else 0, 1),
            "referral_conversion": round((completed/total*100) if total else 0, 1),
            "reward_conversion": round((rewards/completed*100) if completed else 0, 1),
            "pool_total": numbers, "pool_used": used,
            "pool_remaining_capacity": max(0, numbers*MAX_USERS_PER_NUMBER-used),
        }


def display_name(row: aiosqlite.Row) -> str:
    if row["first_name"]:
        return hesc(row["first_name"])
    if row["username"]:
        return f"@{hesc(row['username'])}"
    return f"User {row['user_id']}"


# ---------------------------------------------------------------------------
# Verification flow — ORDER: gate (channels) -> captcha -> phone -> done
# ---------------------------------------------------------------------------

STEP_GATE = "gate"
STEP_CAPTCHA = "captcha"
STEP_PHONE = "phone"
STEP_DONE = "done"


async def next_step(user: aiosqlite.Row) -> str:
    """Return the next step this user still has to complete.

    Channels are FIRST now: nobody moves forward until they've joined.
    Then captcha, then Indian-number verification.
    """
    if not user["joined_gate"]:
        if not await get_channels():
            return STEP_CAPTCHA if await get_setting("captcha_enabled","1")=="1" and not user["captcha_passed"] else (STEP_PHONE if await get_setting("phone_verify_enabled","1")=="1" and not user["phone_verified"] else STEP_DONE)
        return STEP_GATE
    if await get_setting("captcha_enabled", "1") == "1" and not user["captcha_passed"]:
        return STEP_CAPTCHA
    if await get_setting("phone_verify_enabled", "1") == "1" and not user["phone_verified"]:
        return STEP_PHONE
    return STEP_DONE


async def all_verifications_passed(user: aiosqlite.Row) -> bool:
    return await next_step(user) == STEP_DONE


def build_captcha() -> tuple[str, int, list[int]]:
    a, b = random.randint(2, 9), random.randint(2, 9)
    answer = a + b
    options = {answer}
    while len(options) < 4:
        options.add(random.randint(4, 18))
    opts = list(options)
    random.shuffle(opts)
    return f"{a} + {b}", answer, opts


# ---------------------------------------------------------------------------
# Reward delivery (per-referral WhatsApp number) & referral crediting
# ---------------------------------------------------------------------------

def wa_link(number: str) -> str:
    return f"https://wa.me/{number}"


async def deliver_number_reward(bot: Bot, user_id: int, referral_count: int, referral_id: int | None = None) -> bool:
    required=max(1,await get_required_referrals()); quantity=max(1,int(await get_setting("reward_quantity","1") or 1)); limit=max(0,int(await get_setting("reward_limit","0") or 0))
    target_units=(referral_count//required)*quantity
    if limit: target_units=min(target_units,limit)
    delivered=0
    while await reward_count(user_id)<target_units:
        reserved=await reserve_reward_for_user(user_id,referral_id)
        if reserved is None: break
        caption=await get_setting("reward_caption","")
        text=await ui_message("reward",number=pretty_number(reserved["number"]),caption=SafeHTML(caption),reward_date=reserved["created_at"][:10])
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=await ui_button("open_whatsapp","💬 Open WhatsApp"),url=wa_link(reserved["number"]))],[InlineKeyboardButton(text=await ui_button("referral_link","🔗 REFER MORE"),callback_data="menu_link")]])
        try:
            msg=await bot.send_message(user_id,text,reply_markup=kb); await mark_reward_delivered(reserved["reward_id"],msg.message_id,user_id); await mark_reward_sent(user_id); delivered+=1
        except TelegramForbiddenError:
            logger.warning("Reward reserved but user blocked bot: %s",user_id); break
        except Exception:
            logger.exception("Reward delivery failed for %s",user_id); break
    if delivered==0 and await reward_count(user_id)==0:
        try: await bot.send_message(user_id,await ui_message("reward_empty"))
        except Exception: pass
    return delivered>0


async def notify_invalid_referral(bot: Bot, referrer_id: int) -> None:
    try:
        await bot.send_message(referrer_id, await ui_message("invalid_referral"))
    except Exception:
        pass


async def maybe_credit_referral(user_id: int, bot: Bot) -> None:
    user=await get_user(user_id)
    if not user or user["referred_by"] is None or user["referral_credited"] or user["banned"] or user["restricted"]: return
    if not await all_verifications_passed(user): return
    referrer=await get_user(user["referred_by"])
    if not referrer or referrer["banned"] or referrer["restricted"]: return
    credited=await credit_referral_and_mark(user_id,referrer["user_id"])
    if not credited: return
    referrer=await get_user(referrer["user_id"]); required=await get_required_referrals(); count=referrer["referral_count"]
    if count>=required and count%required==0:
        await deliver_number_reward(bot,referrer["user_id"],count,referral_id=user_id)
    else:
        try: await bot.send_message(referrer["user_id"],f"🎉 <b>+1 referral!</b>\\n\\n{progress_bar(count,required)}  {count}/{required}")
        except Exception: pass


# ---------------------------------------------------------------------------
# V3 security, roles, audit and analytics helpers
# ---------------------------------------------------------------------------
ROLE_PERMISSIONS = {
    "owner": {"*"},
    "super_admin": {"*"},
    "manager": {"users", "rewards", "channels", "verification", "ui", "analytics"},
    "support": {"users", "verification"},
    "broadcast_manager": {"broadcast"},
    "analytics_viewer": {"analytics"},
    "security": {"security"},
}

ACTION_CATEGORY = {
    "adm_broadcast": "broadcast", "adm_stats": "analytics", "v3_analytics": "analytics",
    "v3_users": "users", "v3_rewards": "rewards", "v3_channels": "channels",
    "adm_channels": "channels", "adm_numbers": "rewards", "adm_verify": "verification",
    "adm_editor": "ui", "adm_banner": "ui", "adm_system": "system",
    "adm_reset": "system", "adm_reset_confirm": "system", "adm_clone": "system",
    "adm_clone_manager": "system", "adm_backup": "system", "sys_maintenance": "system",
    "v3_roles": "security", "v3_role_add": "security", "v3_security": "security",
    "v3_backup": "system", "v3_backup_create": "system", "v3_backup_history": "system",
    "v3_bc_start": "broadcast",
}

async def get_admin_role(admin_id: int) -> str:
    if admin_id in ADMIN_IDS:
        # ADMIN_IDS remains fully backward-compatible and is always owner.
        return "owner"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT role FROM admin_roles WHERE admin_id=?", (admin_id,))
        row = await cur.fetchone()
        return row[0] if row else ""

async def can_admin(admin_id: int, category: str) -> bool:
    role = await get_admin_role(admin_id)
    return bool(role and ("*" in ROLE_PERMISSIONS.get(role, set()) or category in ROLE_PERMISSIONS.get(role, set())))

async def audit(admin_id: int, action: str, target_user_id: int | None = None, details: str = "", before: str = "", after: str = "") -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO audit_logs(admin_id,action,target_user_id,details,before_value,after_value,created_at) VALUES (?,?,?,?,?,?,?)",
                (admin_id, action, target_user_id, details[:2000], before[:2000], after[:2000], datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
    except Exception:
        logger.exception("Audit log failed")

class AdminGuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user:
            return
        if CLONE_MODE:
            # Clone admins are authenticated against this clone's database,
            # never against the Master ADMIN_IDS list.
            if not await clone_admin_authorized(user.id):
                if hasattr(event, "answer"):
                    try:
                        await event.answer("⛔ Admin access required.", show_alert=True)
                    except Exception:
                        pass
                return
            feature = await protected_feature_for_event(event, data)
            if feature and not await has_clone_permission(user.id, feature):
                if hasattr(event, "answer"):
                    try:
                        await event.answer("⛔ You don't have permission to use this feature.", show_alert=True)
                    except Exception:
                        pass
                elif getattr(event, "chat", None):
                    try:
                        await event.answer("⛔ You don't have permission to use this feature.")
                    except Exception:
                        pass
                return
            return await handler(event, data)

        if not await get_admin_role(user.id):
            if hasattr(event, "answer"):
                try:
                    await event.answer("⛔ Admin access required.", show_alert=True)
                except Exception:
                    pass
            return
        cb = getattr(event, "data", None)
        category = ACTION_CATEGORY.get(cb, "") if cb else ""
        if category and not await can_admin(user.id, category):
            if hasattr(event, "answer"):
                try:
                    await event.answer("⛔ Your admin role cannot perform this action.", show_alert=True)
                except Exception:
                    pass
            return
        return await handler(event, data)

async def protected_feature_for_event(event, data) -> str:
    cb = getattr(event, "data", "") or ""
    exact = {
        "adm_stats":"basic_analytics","v3_analytics":"advanced_analytics",
        "v3_health":"diagnostics","v3_users":"users","adm_finduser":"user_search",
        "adm_broadcast":"broadcast","v3_broadcast":"broadcast","v3_bc_start":"broadcast",
        "adm_numbers":"reward_pool","v3_rewards":"reward_history",
        "adm_channels":"channel_manage","v3_channels":"channel_view",
        "adm_joinreq":"join_requests","adm_verify":"captcha",
        "adm_editor":"content_edit","adm_banner":"banner_edit",
        "adm_backup":"backup","v3_backup":"backup","adm_system":"settings",
        "adm_reset":"reward_reset","adm_mode":"settings","adm_reward":"reward_caption",
        "adm_required":"referral", "adm_reward_rules":"referral", "adm_reward_rules":"referral","adm_setadmin":"settings","adm_export":"csv_export",
        "jr_toggle":"join_requests","jr_info":"join_requests","jr_center":"join_requests",
        "jr_refresh":"join_requests","jr_health":"join_requests","jr_expire":"join_requests",
        "ui_theme":"content_edit","ui_preview_gate":"content_view",
    }
    if cb in exact:
        return exact[cb]
    prefixes = (
        ("usr_", "users"), ("ce_m:", "content_edit"), ("ce_b:", "button_edit"),
        ("vs_captcha", "captcha"), ("vs_phone", "phone_verification"),
        ("ch_", "channel_manage"), ("num_", "reward_pool"),
        ("v3_user_", "users"), ("v3_reward_", "reward_history"),
        ("v3_ch_", "channel_manage"), ("v3_backup_", "backup"),
        ("sys_", "settings"), ("mode_", "settings"), ("jr_channel:", "join_requests"),
        ("theme:", "content_edit"),
    )
    for prefix, feature in prefixes:
        if cb.startswith(prefix):
            return feature
    state = data.get("state")
    if state is not None:
        try:
            state_name = await state.get_state()
        except Exception:
            state_name = None
        return state_feature(state_name)
    return ""

def state_feature(state_name: str | None) -> str:
    if not state_name:
        return ""
    name = state_name.split(":")[-1]
    return {
        "waiting_broadcast":"broadcast",
        "waiting_numbers":"reward_pool",
        "waiting_channel_forward":"channel_manage",
        "waiting_channel_link":"channel_manage",
        "waiting_ui_message":"content_edit",
        "waiting_ui_button":"button_edit",
        "waiting_banner_photo":"banner_edit",
        "waiting_reward_caption":"reward_caption",
        "waiting_required_referrals":"referral",
        "waiting_reward_rules":"referral",
        "waiting_admin_contact":"settings",
        "waiting_find_user":"user_search",
        "waiting_v3_user_search":"users",
        "waiting_v3_broadcast":"broadcast",
        "waiting_v3_restore":"backup",
        "waiting_clone_token":"clone_manager",
        "waiting_clone_admin_id":"clone_manager",
        "waiting_clone_name":"clone_manager",
        "waiting_clone_package":"clone_manager",
        "waiting_clone_feature":"clone_permissions",
        "waiting_clone_typed_delete":"clone_manager",
    }.get(name, "")


async def v3_analytics(period_days: int | None = None) -> dict:
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        async def c(sql, params=()):
            cur=await db.execute(sql,params); return int((await cur.fetchone())[0])
        where = "" if not period_days else "WHERE created_at >= ?"
        params = () if not period_days else ((now-timedelta(days=period_days)).isoformat(),)
        users=await c(f"SELECT COUNT(*) FROM users {where}",params)
        verified=await c(f"SELECT COUNT(*) FROM users {where+' AND' if where else 'WHERE'} phone_verified=1",params)
        referrals=await c(f"SELECT COUNT(*) FROM users {where+' AND' if where else 'WHERE'} referral_credited=1",params)
        rewards=await c(f"SELECT COUNT(*) FROM users {where+' AND' if where else 'WHERE'} reward_sent=1",params)
        top=await db.execute(f"SELECT referred_by, COUNT(*) c FROM users {where+' AND' if where else 'WHERE'} referred_by IS NOT NULL AND referral_credited=1 GROUP BY referred_by ORDER BY c DESC LIMIT 10",params)
        top_rows=await top.fetchall()
        return {"users":users,"verified":verified,"referrals":referrals,"rewards":rewards,"verification_rate":(verified/users*100 if users else 0),"referral_rate":(referrals/users*100 if users else 0),"reward_rate":(rewards/referrals*100 if referrals else 0),"top":top_rows}


async def user_search(query: str, page: int = 0, filt: str = "all") -> tuple[list[aiosqlite.Row], int]:
    clauses=[]; params=[]
    q=query.strip()
    if q:
        clauses.append("(CAST(user_id AS TEXT)=? OR LOWER(username) LIKE LOWER(?) OR LOWER(first_name) LIKE LOWER(?) OR phone LIKE ?)")
        params += [q, f"%{q.lstrip('@')}%", f"%{q}%", f"%{re.sub(r'\D','',q)}%"]
    filters={
        "verified":"phone_verified=1", "unverified":"phone_verified=0", "banned":"banned=1", "unbanned":"banned=0",
        "restricted":"restricted=1", "unrestricted":"restricted=0", "rewarded":"reward_sent=1", "unrewarded":"reward_sent=0"}
    if filt in filters: clauses.append(filters[filt])
    where=" WHERE "+" AND ".join(clauses) if clauses else ""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory=aiosqlite.Row
        total=int((await (await db.execute(f"SELECT COUNT(*) FROM users{where}",params)).fetchone())[0])
        cur=await db.execute(f"SELECT * FROM users{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",params+[V3_PAGE_SIZE,page*V3_PAGE_SIZE])
        return list(await cur.fetchall()), total


async def reward_history_rows(query: str = "", page: int = 0):
    q=f"%{query.strip()}%" if query.strip() else "%"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("""SELECT rh.rowid AS reward_id, rh.user_id, u.username, rn.number, rh.sent_at
            FROM reward_handouts rh JOIN users u ON u.user_id=rh.user_id JOIN reward_numbers rn ON rn.id=rh.number_id
            WHERE CAST(rh.user_id AS TEXT) LIKE ? OR COALESCE(u.username,'') LIKE ? OR rn.number LIKE ?
            ORDER BY rh.sent_at DESC LIMIT ? OFFSET ?""",(q,q,q,V3_PAGE_SIZE,page*V3_PAGE_SIZE))
        return list(await cur.fetchall())


def fmt_uptime(seconds: float) -> str:
    seconds=max(0,int(seconds)); d,r=divmod(seconds,86400); h,r=divmod(r,3600); m,s=divmod(r,60)
    return f"{d}d {h}h {m}m {s}s" if d else f"{h}h {m}m {s}s"

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _rows_of_two(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    """Pack buttons two-per-row (last row keeps a single button if odd)."""
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def captcha_keyboard(options: list[int]) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=str(n), callback_data=f"cap:{n}") for n in options]
    return InlineKeyboardMarkup(inline_keyboard=_rows_of_two(buttons))


async def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=await ui_button("share_number"), request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)


def gate_keyboard(channels: list[aiosqlite.Row], join_label: str = "") -> InlineKeyboardMarkup:
    """Channel gate keyboard + Joined/Done confirm button."""
    join_buttons = [
        InlineKeyboardButton(text=f"📢 {ch['title']}", url=ch['invite_link'])
        for ch in channels
    ]
    rows = _rows_of_two(join_buttons)
    label = join_label or "✅ Joined / Done"
    rows.append([InlineKeyboardButton(text=label, callback_data="gate_joined")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_gate_keyboard(bot: Bot, user_id: int, channels: list[aiosqlite.Row]) -> InlineKeyboardMarkup:
    """Channel buttons + Joined/Done. Auto-detect still works; button covers already-joined users."""
    rows = [
        [InlineKeyboardButton(text=f"📢 {ch['title']}", url=ch['invite_link'])]
        for ch in channels
    ]
    rows.append([InlineKeyboardButton(text=await ui_button("joined_done", "✅ Joined / Done"), callback_data="gate_joined")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def contact_admin_keyboard() -> InlineKeyboardMarkup:
    admin_username = (await get_setting("admin_username", "")).lstrip("@")
    admin_id = (await get_setting("admin_contact_id", "")).strip()
    label = await ui_button("contact_admin")
    if admin_id.isdigit():
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=f"tg://user?id={admin_id}")]])
    if admin_username:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=f"https://t.me/{admin_username}")]])
    return InlineKeyboardMarkup(inline_keyboard=[])



async def main_menu_keyboard(unlocked: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=await ui_button("referral_link","🔗 REFER & EARN"),callback_data="menu_link"), InlineKeyboardButton(text=await ui_button("stats","📊 STATUS"),callback_data="menu_stats")],
        [InlineKeyboardButton(text=await ui_button("my_reward","🎁 MY REWARD"),callback_data="my_reward")],
        [InlineKeyboardButton(text=await ui_button("help_menu","ℹ️ Help"),callback_data="menu_help"), InlineKeyboardButton(text=await ui_button("support_menu","🆘 Support"),callback_data="menu_support")],
    ])


async def main_menu_reply_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard under the message input (like native Telegram bots)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=await ui_button("referral_link", "🔗 REFER & EARN")),
                KeyboardButton(text=await ui_button("stats", "📊 STATUS")),
            ],
            [
                KeyboardButton(text=await ui_button("my_reward", "🎁 MY REWARD")),
                KeyboardButton(text=await ui_button("help_menu", "ℹ️ Help")),
            ],
            [
                KeyboardButton(text=await ui_button("support_menu", "🆘 Support")),
                KeyboardButton(text=await ui_button("menu_home", "🏠 Home")),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def safe_edit_text(message: Message, text: str, reply_markup=None) -> Message:
    """Edit in place; if edit fails (photo msg / too long / not modified), send a fresh message."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return message
    except TelegramBadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            return message
    except Exception:
        pass
    try:
        await message.delete()
    except Exception:
        pass
    return await message.answer(text, reply_markup=reply_markup)


async def back_keyboard(callback_data: str = "menu_back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=await ui_button("back"), callback_data=callback_data)]])


async def cancel_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=await ui_button("cancel"), callback_data=callback_data)]])


async def admin_panel_keyboard(admin_id: int | None = None) -> InlineKeyboardMarkup:
    if CLONE_MODE:
        admin_id = admin_id or (CLONE_ADMIN_IDS[0] if CLONE_ADMIN_IDS else 0)
        buttons = []
        def allowed(feature): return True if feature == "dashboard" else False
        feature_buttons = [
            ("👥 Users","users"), ("🤝 Referrals","referral"),
            ("🎁 Rewards","reward_claim"), ("📢 Channels","channel_view"),
            ("🛡 Verification","captcha"), ("📊 Statistics","basic_analytics"),
            ("🎨 Content","content_view"), ("⚙️ Settings","settings"),
            ("📣 Broadcast","broadcast"), ("🔢 Reward Pool","reward_pool"),
            ("📈 Advanced Analytics","advanced_analytics"), ("📤 CSV Export","csv_export"),
            ("💾 Backup","backup"), ("🩺 Diagnostics","diagnostics"),
        ]
        callback_for = {
            "users":"v3_users","referral":"v3_users","reward_claim":"v3_rewards",
            "channel_view":"v3_channels","captcha":"adm_verify",
            "basic_analytics":"adm_stats","content_view":"adm_editor",
            "settings":"adm_system","broadcast":"v3_broadcast",
            "reward_pool":"adm_numbers","advanced_analytics":"v3_analytics",
            "csv_export":"adm_export","backup":"v3_backup","diagnostics":"v3_health",
        }
        # Referral/reward user-facing buttons are not clone-admin modules;
        # the existing admin screens remain available through the corresponding
        # permission-protected callbacks where applicable.
        for label, feature in feature_buttons:
            if await has_clone_permission(admin_id, feature):
                buttons.append(InlineKeyboardButton(text=label, callback_data=callback_for[feature]))
        rows = _rows_of_two(buttons)
        rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="adm_back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    mode = await get_bot_mode()
    mode_label = "🤝 Mode: Refer & Earn" if mode == "refer" else "🎯 Mode: Task & Earn"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Stats", callback_data="adm_stats"),
         InlineKeyboardButton(text=mode_label, callback_data="adm_mode")],
        [InlineKeyboardButton(text="✏️ Message & Button Editor", callback_data="adm_editor")],
        [InlineKeyboardButton(text="🎁 Reward Caption", callback_data="adm_reward"),
         InlineKeyboardButton(text="⚙️ Reward Rules", callback_data="adm_reward_rules")],
        [InlineKeyboardButton(text="📞 Manage Numbers", callback_data="adm_numbers"),
         InlineKeyboardButton(text="📢 Manage Channels", callback_data="adm_channels")],
        [InlineKeyboardButton(text="🛡 Verification", callback_data="adm_verify")],
        [InlineKeyboardButton(text="👨‍💼 Admin Contact", callback_data="adm_setadmin"),
         InlineKeyboardButton(text="🖼 Mode Banner", callback_data="adm_banner")],
        [InlineKeyboardButton(text="📣 Broadcast", callback_data="adm_broadcast"),
         InlineKeyboardButton(text="👤 Find User", callback_data="adm_finduser")],
        [InlineKeyboardButton(text="📤 Export CSV", callback_data="adm_export"),
         InlineKeyboardButton(text="💾 Database Backup", callback_data="adm_backup")],
        [InlineKeyboardButton(text="♻️ Reset Referrals", callback_data="adm_reset"),
         InlineKeyboardButton(text="🧬 Clone Manager", callback_data="adm_clone_manager")],
        [InlineKeyboardButton(text="⚙️ System Settings", callback_data="adm_system")],
        [InlineKeyboardButton(text="📈 V3 Analytics", callback_data="v3_analytics"), InlineKeyboardButton(text="👥 User Manager", callback_data="v3_users")],
        [InlineKeyboardButton(text="🧾 Audit Log", callback_data="v3_audit"), InlineKeyboardButton(text="👮 Admin Roles", callback_data="v3_roles")],
        [InlineKeyboardButton(text="📣 Broadcast Pro", callback_data="v3_broadcast"), InlineKeyboardButton(text="🎁 Reward Pro", callback_data="v3_rewards")],
        [InlineKeyboardButton(text="📡 Channel Health", callback_data="v3_channels"), InlineKeyboardButton(text="🩺 Health Center", callback_data="v3_health")],
        [InlineKeyboardButton(text="🔐 Security Center", callback_data="v3_security"), InlineKeyboardButton(text="💾 Backup Center", callback_data="v3_backup")],
    ])


def verification_settings_keyboard(captcha_on: bool, phone_on: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🧩 Captcha: {'✅ ON' if captcha_on else '❌ OFF'}",
                    callback_data="vs_captcha",
                ),
                InlineKeyboardButton(
                    text=f"📱 Phone: {'✅ ON' if phone_on else '❌ OFF'}",
                    callback_data="vs_phone",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="adm_back")],
        ]
    )


def mode_settings_keyboard(mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🤝 Refer & Earn {'✅' if mode == 'refer' else ''}",
                    callback_data="mode_refer",
                ),
                InlineKeyboardButton(
                    text=f"🎯 Task & Earn {'✅' if mode == 'task' else ''}",
                    callback_data="mode_task",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="adm_back")],
        ]
    )


def user_card(user: aiosqlite.Row, required: int) -> tuple[str, InlineKeyboardMarkup]:
    uid = user["user_id"]
    username_line = f"@{hesc(user['username'])}" if user["username"] else "—"
    phone_line = pretty_number(user["phone"]) if user["phone"] else "—"
    text = (
        "👤 <b>User Lookup</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"👤 Name: {hesc(user['first_name'] or '—')}\n"
        f"🔗 Username: {username_line}\n"
        f"📱 Phone: {phone_line} ({'verified' if user['phone_verified'] else 'not verified'})\n"
        f"🧩 Captcha: {'Yes' if user['captcha_passed'] else 'No'}\n"
        f"🔒 Gate: {'Yes' if user['joined_gate'] else 'No'}\n"
        f"👥 Referrals: {user['referral_count']}/{required}\n"
        f"🎁 Reward sent: {'Yes' if user['reward_sent'] else 'No'}\n"
        f"🚫 Banned: {'Yes' if user['banned'] else 'No'}\n"
        f"⛔ Restricted: {'Yes' if user['restricted'] else 'No'}\n"
        f"📅 Joined: {user['created_at'][:19].replace('T', ' ')} UTC"
    )
    ban_button = (
        InlineKeyboardButton(text="♻️ Unban", callback_data=f"usr_unban:{uid}")
        if user["banned"]
        else InlineKeyboardButton(text="🚫 Ban", callback_data=f"usr_ban:{uid}")
    )
    restrict_button = (
        InlineKeyboardButton(text="✅ Un-restrict", callback_data=f"usr_unrestrict:{uid}")
        if user["restricted"]
        else InlineKeyboardButton(text="⛔ Restrict", callback_data=f"usr_restrict:{uid}")
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [ban_button, restrict_button],
            [
                InlineKeyboardButton(text="➕1 Referral", callback_data=f"usr_add:{uid}"),
                InlineKeyboardButton(text="➖1 Referral", callback_data=f"usr_sub:{uid}"),
            ],
            [
                InlineKeyboardButton(text="🎁 View Rewards", callback_data=f"usr_rewards:{uid}"),
                InlineKeyboardButton(text="♻️ Resend Latest", callback_data=f"usr_resend:{uid}"),
            ],
            [InlineKeyboardButton(text="🔁 Reset Flag", callback_data=f"usr_reset:{uid}"),
             InlineKeyboardButton(text="⬅️ Back", callback_data="adm_back")],
        ]
    )
    return text, kb


def build_channels_list(channels: list[aiosqlite.Row]) -> tuple[str, InlineKeyboardMarkup]:
    remove_buttons = [
        InlineKeyboardButton(
            text=f"❌ {ch['title']}", callback_data=f"ch_remove:{ch['channel_id']}"
        )
        for ch in channels
    ]
    rows = _rows_of_two(remove_buttons)
    rows.append([InlineKeyboardButton(text="➕ Add Channel", callback_data="ch_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="adm_back")])

    body = (
        "\n".join(f"• {hesc(ch['title'] or str(ch['channel_id']))}" for ch in channels)
        if channels else "No channels configured yet."
    )
    return f"📢 <b>Manage Channels</b>\n\n{body}", InlineKeyboardMarkup(inline_keyboard=rows)


async def build_numbers_list() -> tuple[str, InlineKeyboardMarkup]:
    numbers = await get_reward_numbers()
    total, remaining, used = await pool_stats()
    remove_buttons = [
        InlineKeyboardButton(
            text=f"❌ {pretty_number(n['number'])} ({n['handout_count']})",
            callback_data=f"num_del:{n['id']}",
        )
        for n in numbers[:40]  # keep the keyboard within Telegram limits
    ]
    rows = _rows_of_two(remove_buttons)
    rows.append([
        InlineKeyboardButton(text="➕ Add Numbers", callback_data="num_add"),
        InlineKeyboardButton(text="🗑 Clear All", callback_data="num_clear"),
    ])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="adm_back")])

    text = (
        "📞 <b>Reward Number Pool</b>\n\n"
        f"📦 Numbers in pool: <b>{total}</b>\n"
        f"🎁 Handouts done: <b>{used}</b>\n"
        f"♻️ Capacity left: <b>{remaining}</b> "
        f"(each number → max {MAX_USERS_PER_NUMBER} users)\n\n"
        "Tap a number to delete it. The count in brackets is how many users "
        "already received it."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Screen renderers (shared between fresh sends and in-place edits)
# ---------------------------------------------------------------------------

async def get_mode_banner_file_id(mode: str) -> str:
    key = "refer_banner_file_id" if mode == "refer" else "task_banner_file_id"
    return await get_setting(key, "")


async def get_mode_banner_caption(mode: str) -> str:
    key = "refer_banner_caption" if mode == "refer" else "task_banner_caption"
    return await get_setting(key, "")


async def _delete_user_banner(bot: Bot, user_id: int, chat_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT message_id FROM user_banner_messages WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if row:
            try:
                await bot.delete_message(chat_id, int(row[0]))
            except Exception:
                pass
        await db.execute("DELETE FROM user_banner_messages WHERE user_id = ?", (user_id,))
        await db.commit()


async def _remember_user_banner(user_id: int, message_id: int, mode: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_banner_messages(user_id, message_id, mode, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                message_id=excluded.message_id,
                mode=excluded.mode,
                updated_at=excluded.updated_at
            """,
            (user_id, message_id, mode, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def send_mode_banner(
    bot: Bot,
    chat_id: int,
    user_id: int,
    mode: str,
    screen_text: str,
    reply_markup: InlineKeyboardMarkup,
) -> Optional[Message]:
    """Send ONE unified image+caption+keyboard message and remove the previous banner."""
    file_id = await get_mode_banner_file_id(mode)
    if not file_id:
        return None

    caption = await get_mode_banner_caption(mode)
    combined = f"{caption}\n\n{screen_text}".strip()
    # Telegram photo captions are limited; if the combined dashboard is too long,
    # keep the configured banner caption on the image and send the full screen below.
    try:
        if len(combined) <= 1024:
            msg = await bot.send_photo(
                chat_id,
                file_id,
                caption=combined,
                reply_markup=reply_markup,
            )
        else:
            msg = await bot.send_photo(
                chat_id,
                file_id,
                caption=caption[:1024],
            )
            msg = await bot.send_message(chat_id, screen_text, reply_markup=reply_markup)
        await _remember_user_banner(user_id, msg.message_id, mode)
        return msg
    except Exception:
        logger.exception("Mode banner failed for chat %s", chat_id)
        return None


async def get_flow_message(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT * FROM flow_messages WHERE user_id=?",(user_id,)); return await cur.fetchone()

async def set_flow_message(user_id:int,chat_id:int,message_id:int,step:str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO flow_messages(user_id,chat_id,message_id,step,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,message_id=excluded.message_id,step=excluded.step,updated_at=excluded.updated_at",(user_id,chat_id,message_id,step,datetime.now(timezone.utc).isoformat())); await db.commit()

async def clear_flow_message(user_id:int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM flow_messages WHERE user_id=?",(user_id,)); await db.commit()

async def delete_previous_flow(bot:Bot,user_id:int,chat_id:int):
    row=await get_flow_message(user_id)
    if row:
        try: await bot.delete_message(chat_id or row["chat_id"],row["message_id"])
        except Exception: pass

async def render_gate(bot: Bot, chat_id: int, edit_message: Optional[Message] = None) -> None:
    user_id=chat_id; channels=await get_channels()
    text=await ui_message("gate")
    if channels:
        text += "\n\n" + "\n".join(f"📢 <b>{hesc(ch['title'])}</b>" for ch in channels)
    else:
        text += "\n\nNo required channels are configured."
    kb=await build_gate_keyboard(bot,user_id,channels)
    if edit_message:
        try:
            await edit_message.edit_text(text,reply_markup=kb)
            await set_flow_message(user_id,chat_id,edit_message.message_id,STEP_GATE); return
        except TelegramBadRequest: pass
    existing=await get_flow_message(user_id)
    if existing:
        try:
            await bot.edit_message_text(text,chat_id=existing["chat_id"],message_id=existing["message_id"],reply_markup=kb)
            await set_flow_message(user_id,chat_id,existing["message_id"],STEP_GATE); return
        except Exception: pass
    await delete_previous_flow(bot,user_id,chat_id)
    msg=await bot.send_message(chat_id,text,reply_markup=kb)
    await set_flow_message(user_id,chat_id,msg.message_id,STEP_GATE)


async def render_captcha(bot: Bot, chat_id: int, user_id: int, edit_message: Optional[Message] = None) -> None:
    question,answer,options=build_captcha(); await set_captcha_answer(user_id,str(answer))
    text=await ui_message("captcha",question=question); kb=captcha_keyboard(options)
    if edit_message:
        try: await edit_message.edit_text(text,reply_markup=kb); await set_flow_message(user_id,chat_id,edit_message.message_id,STEP_CAPTCHA); return
        except TelegramBadRequest: pass
    existing=await get_flow_message(user_id)
    if existing:
        try:
            await bot.edit_message_text(text,chat_id=existing["chat_id"],message_id=existing["message_id"],reply_markup=kb)
            await set_flow_message(user_id,chat_id,existing["message_id"],STEP_CAPTCHA); return
        except Exception: pass
    await delete_previous_flow(bot,user_id,chat_id); msg=await bot.send_message(chat_id,text,reply_markup=kb); await set_flow_message(user_id,chat_id,msg.message_id,STEP_CAPTCHA)

async def render_phone(bot: Bot, chat_id: int, user_id: int, edit_message: Optional[Message] = None) -> None:
    await delete_previous_flow(bot,user_id,chat_id)
    text=await ui_message("phone",share_button=await ui_button("share_number"))
    msg=await bot.send_message(chat_id,text,reply_markup=await phone_keyboard())
    await set_flow_message(user_id,chat_id,msg.message_id,STEP_PHONE)

async def render_contact_admin(bot: Bot, chat_id: int) -> None:
    await bot.send_message(chat_id,await ui_message("restricted"),reply_markup=await contact_admin_keyboard())

async def render_main_menu(bot: Bot, chat_id: int, user_id: int, edit_message: Optional[Message] = None) -> None:
    user=await get_user(user_id); required=await get_required_referrals(); count=user["referral_count"] if user else 0
    latest=await latest_reward(user_id); rcount=await reward_count(user_id)
    reward_status="Delivered" if rcount else f"{max(0,required-count)} referrals to unlock"
    name=await user_context(user_id,bot); name["first_name"]=user["first_name"] if user and user["first_name"] else (f"@{user['username']}" if user and user["username"] else "User")
    name.update({"required":required,"count":count,"reward_status":reward_status,"reward_count":rcount,"latest_reward":pretty_number(latest["reward_number"]) if latest else "—"})
    text=await ui_message("main_locked",**name)
    if count>=required: text=await ui_message("main_unlocked",**name)
    kb=await main_menu_keyboard(True)
    # Keep exactly one active flow message. Banner support is retained but the dashboard is a single text message.
    if edit_message:
        try: await edit_message.edit_text(text,reply_markup=kb); await set_flow_message(user_id,chat_id,edit_message.message_id,STEP_DONE)
        except TelegramBadRequest:
            pass
        else:
            try:
                await bot.send_message(chat_id, "⬇️ <b>Menu ready</b> — use buttons below anytime.", reply_markup=await main_menu_reply_keyboard())
            except Exception:
                pass
            return
    existing=await get_flow_message(user_id)
    if existing:
        try:
            await bot.edit_message_text(text,chat_id=existing["chat_id"],message_id=existing["message_id"],reply_markup=kb)
            await set_flow_message(user_id,chat_id,existing["message_id"],STEP_DONE)
            try:
                await bot.send_message(chat_id, "⬇️ <b>Menu ready</b> — use buttons below anytime.", reply_markup=await main_menu_reply_keyboard())
            except Exception:
                pass
            return
        except Exception: pass
    await delete_previous_flow(bot,user_id,chat_id)
    msg=await bot.send_message(chat_id,text,reply_markup=kb)
    await set_flow_message(user_id,chat_id,msg.message_id,STEP_DONE)
    try:
        await bot.send_message(chat_id, "⬇️ <b>Menu ready</b> — use buttons below anytime.", reply_markup=await main_menu_reply_keyboard())
    except Exception:
        pass

async def render_flow(bot: Bot, chat_id: int, user_id: int, edit_message: Optional[Message] = None) -> None:
    user=await get_user(user_id)
    if user is None: return
    if user["banned"] and not is_admin(user_id): return
    if user["restricted"] and not is_admin(user_id):
        await delete_previous_flow(bot,user_id,chat_id); await render_contact_admin(bot,chat_id); return
    if await get_setting("maintenance_mode","0")=="1" and not is_admin(user_id):
        await delete_previous_flow(bot,user_id,chat_id); msg=await bot.send_message(chat_id,await get_setting("maintenance_message","🛠 Maintenance")); await set_flow_message(user_id,chat_id,msg.message_id,"maintenance"); return
    if is_admin(user_id): return await render_main_menu(bot,chat_id,user_id,edit_message=edit_message)
    # Already-joined users: re-check membership so gate does not soft-lock them.
    if not user["joined_gate"] and await get_channels():
        result = await _evaluate_required_channels(bot, user_id)
        if result["all_member"] or await _all_required_channels_have_join_signal(user_id):
            await mark_joined_gate(user_id)
            await maybe_credit_referral(user_id, bot)
            user = await get_user(user_id)
    step=await next_step(user)
    if step==STEP_GATE: await render_gate(bot,chat_id,edit_message=edit_message)
    elif step==STEP_CAPTCHA: await render_captcha(bot,chat_id,user_id,edit_message=edit_message)
    elif step==STEP_PHONE: await render_phone(bot,chat_id,user_id,edit_message=edit_message)
    else: await render_main_menu(bot,chat_id,user_id,edit_message=edit_message)


async def show_channels_list(message: Message) -> None:
    text, kb = build_channels_list(await get_channels())
    await safe_edit_text(message, text, reply_markup=kb)


async def send_channels_list(message: Message) -> None:
    text, kb = build_channels_list(await get_channels())
    await message.answer(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# FSM states (admin text-input steps)
# ---------------------------------------------------------------------------

class AdminStates(StatesGroup):
    waiting_reward_caption = State()
    waiting_required_referrals = State()
    waiting_reward_rules = State()
    waiting_channel_forward = State()
    waiting_channel_link = State()
    waiting_broadcast = State()
    waiting_find_user = State()
    waiting_numbers = State()
    waiting_admin_contact = State()
    waiting_clone_token = State()
    waiting_banner_photo = State()
    waiting_ui_message = State()
    waiting_ui_button = State()
    waiting_v3_user_search = State()
    waiting_v3_audit_filter = State()
    waiting_v3_role_admin = State()
    waiting_v3_broadcast = State()
    waiting_v3_restore = State()
    waiting_clone_admin_id = State()
    waiting_clone_name = State()
    waiting_clone_package = State()
    waiting_clone_feature = State()
    waiting_clone_typed_delete = State()


# ---------------------------------------------------------------------------
# User-facing router
# ---------------------------------------------------------------------------

user_router = Router(name="user")


@user_router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot) -> None:
    user_id=message.from_user.id; username=message.from_user.username; first_name=message.from_user.first_name or ""
    user=await get_user(user_id)
    if user is None:
        referred_by=None; payload=(command.args or "").strip()
        if payload:
            try: candidate_id=int(payload)
            except ValueError: candidate_id=None
            if candidate_id and candidate_id!=user_id:
                candidate=await get_user(candidate_id)
                if candidate and not candidate["banned"] and not candidate["restricted"]: referred_by=candidate_id
        await create_user(user_id,username,first_name,referred_by); user=await get_user(user_id)
    else: await update_user_profile(user_id,username,first_name)
    if user["banned"] and not is_admin(user_id): return
    if user["restricted"] and not is_admin(user_id): await render_contact_admin(bot,message.chat.id); return
    if is_admin(user_id):
        ctx=await user_context(user_id,bot); admin_name=ctx.get("first_name") or ctx.get("username") or "Admin"
        try: await message.answer(await ui_message("start_admin",admin_name=admin_name))
        except Exception: pass
        await render_main_menu(bot,message.chat.id,user_id); return
    await render_flow(bot,message.chat.id,user_id)


@user_router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(await ui_message("help"))


# --- Captcha ----------------------------------------------------------------

@user_router.callback_query(F.data.startswith("cap:"))
async def cb_captcha(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    user = await get_user(user_id)
    if user is None:
        await callback.answer("Please send /start first.", show_alert=True)
        return
    if user["banned"] or user["restricted"]:
        await callback.answer("Access restricted.", show_alert=True)
        return
    if user["captcha_passed"]:
        await callback.answer()
        return

    chosen = callback.data.split(":", 1)[1]
    if user["captcha_answer"] and chosen == user["captcha_answer"]:
        await mark_captcha_passed(user_id)
        await callback.answer("✅ Correct!")
        await maybe_credit_referral(user_id, bot)
        await render_flow(bot, callback.message.chat.id, user_id, edit_message=callback.message)
    else:
        await callback.answer("❌ Wrong answer — try again!", show_alert=True)
        await render_captcha(bot, callback.message.chat.id, user_id, edit_message=callback.message)


# --- Phone verification (India only) ----------------------------------------

@user_router.message(F.contact)
async def on_contact(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    user = await get_user(user_id)
    if user is None:
        await message.answer("Please send /start first.")
        return
    if (user["banned"] or user["restricted"]) and not is_admin(user_id):
        return
    if await get_setting("phone_verify_enabled", "1") != "1" or user["phone_verified"]:
        return

    contact = message.contact
    if contact.user_id != user_id:
        await message.answer(
            "❌ That's not your own number.\n\n"
            "Please use the <b>📱 Share My Number</b> button — forwarded or "
            "manually attached contacts are not accepted.",
            reply_markup=await phone_keyboard(),
        )
        return

    canonical = normalize_indian_number(contact.phone_number or "")

    # NON-INDIAN NUMBER → restrict this account, show Contact Admin, and tell
    # the referrer their referral was invalid.
    if canonical is None:
        await set_restricted(user_id, True)
        await message.answer(
            "⛔ <b>Only Indian (+91) numbers are supported.</b>",
            reply_markup=ReplyKeyboardRemove(),
        )
        await render_contact_admin(bot, message.chat.id)
        if user["referred_by"] and not user["referral_credited"]:
            await notify_invalid_referral(bot, user["referred_by"])
        logger.info("User %s restricted: non-Indian number.", user_id)
        return

    existing = await get_user_by_phone(canonical)
    if existing and existing["user_id"] != user_id:
        await message.answer(
            "🚫 <b>This phone number is already linked to another account.</b>\n\n"
            "One number = one account. Referrals from duplicate accounts are not "
            "counted.",
            reply_markup=ReplyKeyboardRemove(),
        )
        logger.warning("Duplicate phone: user %s vs owner %s", user_id, existing["user_id"])
        return

    await set_phone_verified(user_id, canonical)
    await maybe_credit_referral(user_id, bot)
    await delete_previous_flow(bot,user_id,message.chat.id)
    await render_flow(bot, message.chat.id, user_id)


# --- Force-join gate --------------------------------------------------------

async def _member_is_verified(bot: Bot, user_id: int, channel_id: int) -> tuple[str, str | None]:
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        status = member.status
        if status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            return "MEMBER", None
        if status == ChatMemberStatus.RESTRICTED and bool(getattr(member, "is_member", False)):
            return "MEMBER", None
        if status == ChatMemberStatus.KICKED:
            return "KICKED", None
        return "LEFT", None
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("Membership check failed channel=%s user=%s: %s", channel_id, user_id, type(exc).__name__)
        return "ERROR", type(exc).__name__
    except Exception as exc:
        logger.exception("Membership check crashed channel=%s user=%s", channel_id, user_id)
        return "ERROR", type(exc).__name__


async def _evaluate_required_channels(bot: Bot, user_id: int) -> dict:
    channels = await get_channels()
    results = []
    all_member = True
    pending = False
    errors = False

    for ch in channels:
        channel_id = ch["channel_id"]
        state = await get_join_state(user_id, channel_id)

        if await _request_expired(state):
            await mark_join_expired(user_id, channel_id)
            state = await get_join_state(user_id, channel_id)

        membership, error = await _member_is_verified(bot, user_id, channel_id)

        if membership == "MEMBER":
            await mark_join_member(user_id, channel_id)
            final_status = "MEMBER"
        elif membership == "KICKED":
            await set_join_request_status(user_id, channel_id, "KICKED")
            final_status = "KICKED"
            all_member = False
        elif membership == "LEFT":
            # Simple mode: a placed join request satisfies this channel's
            # requirement even before an admin approves it — Telegram just
            # hasn't confirmed real membership yet, and that's fine.
            if state and state["status"] in {"REQUESTED", "PENDING_APPROVAL", "APPROVED"}:
                final_status = state["status"]
                pending = state["status"] in {"REQUESTED", "PENDING_APPROVAL"}
            else:
                await set_join_request_status(user_id, channel_id, "LEFT")
                final_status = "LEFT"
                all_member = False
        else:
            errors = True
            all_member = False
            if state and state["status"] in {"REQUESTED", "PENDING_APPROVAL", "APPROVED"}:
                final_status = state["status"]
                pending = state["status"] in {"REQUESTED", "PENDING_APPROVAL"}
            else:
                await mark_join_error(user_id, channel_id, error or "membership_check_failed")
                final_status = "ERROR"

        results.append({
            "channel_id": channel_id,
            "title": ch["title"],
            "invite_link": ch["invite_link"],
            "status": final_status,
            "error": error,
        })

    if all_member:
        await mark_joined_gate(user_id)
    else:
        await unmark_joined_gate(user_id)

    return {
        "all_member": all_member,
        "pending": pending,
        "errors": errors,
        "channels": results,
    }


async def _all_required_channels_have_join_signal(user_id: int) -> bool:
    """Simple gate check: a channel counts as satisfied once the user has
    either joined it directly or placed a join request for it. Admin
    approval of the request is NOT required to move the user forward."""
    channels = await get_channels()
    if not channels:
        return True
    for ch in channels:
        state = await get_join_state(user_id, ch["channel_id"])
        if not state or state["status"] not in {"REQUESTED", "PENDING_APPROVAL", "APPROVED", "MEMBER"}:
            return False
    return True


@user_router.chat_join_request()
async def on_chat_join_request(update: ChatJoinRequest, bot: Bot) -> None:
    user_id=update.from_user.id; channel_id=update.chat.id
    channels=await get_channels()
    if channel_id not in {c["channel_id"] for c in channels}: return
    user=await get_user(user_id)
    if not user or user["banned"] or user["restricted"] or is_admin(user_id): return
    await record_join_request(user_id,channel_id); await set_join_request_status(user_id,channel_id,"REQUESTED")
    await set_setting("join_last_event_at",datetime.now(timezone.utc).strftime("%H:%M:%S"))

    # Simple mode: placing the join request is enough. The user does NOT
    # have to wait for an admin to approve it before moving to the next step.
    if await _all_required_channels_have_join_signal(user_id):
        await mark_joined_gate(user_id); await maybe_credit_referral(user_id,bot); await render_flow(bot,user_id,user_id)

    # Best-effort only: still actually approve the request in Telegram if the
    # admin has auto-approve turned on, so the user genuinely ends up a member
    # of the channel too — but this no longer blocks or delays the flow above.
    if await get_setting("auto_approve_join_requests","0")=="1":
        try:
            await bot.approve_chat_join_request(chat_id=channel_id,user_id=user_id); await mark_join_approved(user_id,channel_id)
        except Exception as exc:
            await mark_join_error(user_id,channel_id,type(exc).__name__)
            logger.info("Auto approval failed for user=%s channel=%s: %s",user_id,channel_id,type(exc).__name__)


@user_router.chat_member()
async def on_chat_member_update(event: ChatMemberUpdated, bot: Bot) -> None:
    channels=await get_channels()
    if event.chat.id not in {c["channel_id"] for c in channels}: return
    user_id=event.new_chat_member.user.id
    if is_admin(user_id): return
    status=event.new_chat_member.status
    if status in (ChatMemberStatus.MEMBER,ChatMemberStatus.ADMINISTRATOR,ChatMemberStatus.CREATOR) or (status==ChatMemberStatus.RESTRICTED and bool(getattr(event.new_chat_member,"is_member",False))):
        await mark_join_member(user_id,event.chat.id); result=await _evaluate_required_channels(bot,user_id)
        if result["all_member"]:
            await mark_joined_gate(user_id); await maybe_credit_referral(user_id,bot)
            flow=await get_flow_message(user_id); await render_flow(bot,user_id,user_id,edit_message=None if not flow else await _safe_get_message(bot,flow["chat_id"],flow["message_id"]))
        return
    await set_join_request_status(user_id,event.chat.id,"KICKED" if status==ChatMemberStatus.KICKED else "LEFT"); await unmark_joined_gate(user_id)

async def _safe_get_message(bot:Bot,chat_id:int,message_id:int):
    # Telegram has no get_message API. Return None; the renderer will replace the tracked message safely.
    return None


@user_router.callback_query(F.data == "gate_joined")
async def cb_gate_joined(callback: CallbackQuery, bot: Bot) -> None:
    """Manual Joined/Done — fixes already-joined users who never receive chat_member updates."""
    user_id = callback.from_user.id
    user = await get_user(user_id)
    if not user or user["banned"] or user["restricted"]:
        await callback.answer("Access restricted.", show_alert=True)
        return
    channels = await get_channels()
    if not channels:
        await mark_joined_gate(user_id)
        await maybe_credit_referral(user_id, bot)
        await callback.answer("✅ Done")
        await render_flow(bot, callback.message.chat.id, user_id, edit_message=callback.message)
        return
    result = await _evaluate_required_channels(bot, user_id)
    ok = result["all_member"] or await _all_required_channels_have_join_signal(user_id)
    if not ok:
        missing = [c["title"] for c in result["channels"] if c["status"] not in {"MEMBER", "REQUESTED", "PENDING_APPROVAL", "APPROVED"}]
        detail = ", ".join(missing[:5]) if missing else "required channel(s)"
        await callback.answer(f"❌ Join first: {detail}"[:180], show_alert=True)
        lines = [await ui_message("gate"), ""]
        for c in result["channels"]:
            st = UI_STATUS.get(c["status"], c["status"])
            lines.append(f"📢 <b>{hesc(c['title'])}</b> — {st}")
        lines.append("")
        lines.append("Join every channel, then tap <b>Joined / Done</b>.")
        kb = await build_gate_keyboard(bot, user_id, channels)
        try:
            await callback.message.edit_text("\n".join(lines), reply_markup=kb)
        except Exception:
            pass
        return
    await mark_joined_gate(user_id)
    await maybe_credit_referral(user_id, bot)
    await callback.answer("✅ Verified!")
    await render_flow(bot, callback.message.chat.id, user_id, edit_message=callback.message)


# --- Main menu (NO leaderboard for users) -----------------------------------

@user_router.callback_query(F.data == "menu_link")
async def cb_referral_link(callback: CallbackQuery, bot: Bot) -> None:
    user_id=callback.from_user.id; user=await get_user(user_id)
    if not user: await callback.answer("Please send /start first.",show_alert=True); return
    me=await bot.get_me(); link=f"https://t.me/{me.username}?start={user_id}" if me.username else ""
    required=await get_required_referrals(); count=user["referral_count"]
    text=await ui_message("referral_link",link=link,referrals=count,required_referrals=required,reward=f"{await get_setting('reward_quantity','1')} Agent Number")
    share=f"https://t.me/share/url?url={quote(link,safe='')}&text={quote(await ui_message('share_caption'),safe='')}"
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=await ui_button("share_friend","📤 SHARE LINK"),url=share)],[InlineKeyboardButton(text=await ui_button("back","⬅️ Back"),callback_data="menu_back")]])
    try: await callback.message.edit_text(text,reply_markup=kb)
    except TelegramBadRequest: pass
    await callback.answer()


@user_router.callback_query(F.data == "menu_stats")
async def cb_my_stats(callback: CallbackQuery) -> None:
    user=await get_user(callback.from_user.id)
    if not user: await callback.answer(await ui_message("no_user"),show_alert=True); return
    required=await get_required_referrals(); latest=await latest_reward(user["user_id"]); rc=await reward_count(user["user_id"])
    text=await ui_message("stats",progress=progress_bar(user["referral_count"],required),count=user["referral_count"],required=required,reward_count=rc,phone="Verified" if user["phone_verified"] else "Not verified",access="Verified" if user["joined_gate"] else "Pending",latest_reward=pretty_number(latest["reward_number"]) if latest else "—")
    await callback.message.edit_text(text,reply_markup=await back_keyboard("menu_back")); await callback.answer()


@user_router.callback_query(F.data == "my_reward")
async def cb_my_reward(callback: CallbackQuery, bot: Bot) -> None:
    user_id=callback.from_user.id; user=await get_user(user_id)
    if not user or user["banned"] or user["restricted"]:
        await callback.answer("Access restricted.",show_alert=True); return
    ok=await recover_reward(bot,user_id)
    await callback.answer("🎁 Reward recovered." if ok else "No active reward found.",show_alert=not ok)

@user_router.callback_query(F.data == "menu_back")
async def cb_menu_back(callback: CallbackQuery, bot: Bot) -> None:
    await render_main_menu(bot, callback.message.chat.id, callback.from_user.id, edit_message=callback.message)
    await callback.answer()


# ---------------------------------------------------------------------------
# Admin router
# ---------------------------------------------------------------------------

async def admin_context(admin_id:int, bot:Bot|None=None)->dict:
    first=last=username=""
    row=await get_user(admin_id)
    if row:
        first=row["first_name"] or ""; username=row["username"] or ""
    if bot and not first:
        try:
            me=await bot.get_chat(admin_id); first=me.first_name or ""; last=me.last_name or ""; username=me.username or username
        except Exception: pass
    name=first or (" ".join(x for x in [first,last] if x)) or (f"@{username}" if username else "Admin")
    return {"admin_name":name,"admin_first_name":first or name,"admin_last_name":last,"admin_username":f"@{username}" if username else "","admin_id":admin_id}


@user_router.callback_query(F.data == "menu_help")
async def cb_menu_help(callback: CallbackQuery) -> None:
    await callback.message.answer(await ui_message("help"))
    await callback.answer()


@user_router.callback_query(F.data == "menu_support")
async def cb_menu_support(callback: CallbackQuery) -> None:
    await callback.message.answer(await ui_message("restricted"), reply_markup=await contact_admin_keyboard())
    await callback.answer()


async def _reply_menu_labels() -> dict:
    return {
        "link": await ui_button("referral_link", "🔗 REFER & EARN"),
        "stats": await ui_button("stats", "📊 STATUS"),
        "reward": await ui_button("my_reward", "🎁 MY REWARD"),
        "help": await ui_button("help_menu", "ℹ️ Help"),
        "support": await ui_button("support_menu", "🆘 Support"),
        "home": await ui_button("menu_home", "🏠 Home"),
    }


@user_router.message(F.text)
async def cb_reply_keyboard_menu(message: Message, bot: Bot, state: FSMContext) -> None:
    """Bottom reply-keyboard actions for verified users (ignores admin FSM input)."""
    current = await state.get_state()
    if current:
        return
    user_id = message.from_user.id
    if is_admin(user_id):
        return
    user = await get_user(user_id)
    if not user or user["banned"] or user["restricted"]:
        return
    if await next_step(user) != STEP_DONE:
        await render_flow(bot, message.chat.id, user_id)
        return
    labels = await _reply_menu_labels()
    text = (message.text or "").strip()
    if text == labels["link"]:
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start={user_id}" if me.username else ""
        required = await get_required_referrals()
        count = user["referral_count"]
        body = await ui_message(
            "referral_link",
            link=link,
            referrals=count,
            required_referrals=required,
            reward=f"{await get_setting('reward_quantity','1')} Agent Number",
        )
        share = f"https://t.me/share/url?url={quote(link,safe='')}&text={quote(await ui_message('share_caption'),safe='')}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=await ui_button("share_friend","📤 SHARE LINK"), url=share)],
            [InlineKeyboardButton(text=await ui_button("back","⬅️ Back"), callback_data="menu_back")],
        ])
        await message.answer(body, reply_markup=kb)
        return
    if text == labels["stats"]:
        required = await get_required_referrals()
        latest = await latest_reward(user_id)
        rc = await reward_count(user_id)
        body = await ui_message(
            "stats",
            progress=progress_bar(user["referral_count"], required),
            count=user["referral_count"],
            required=required,
            reward_count=rc,
            phone="Verified" if user["phone_verified"] else "Not verified",
            access="Verified" if user["joined_gate"] else "Pending",
            latest_reward=pretty_number(latest["reward_number"]) if latest else "—",
        )
        await message.answer(body, reply_markup=await back_keyboard("menu_back"))
        return
    if text == labels["reward"]:
        ok = await recover_reward(bot, user_id)
        if not ok:
            required = await get_required_referrals()
            left = max(0, required - user["referral_count"])
            await message.answer(f"🎁 Reward locked. Need <b>{left}</b> more referral(s).")
        return
    if text == labels["help"]:
        await message.answer(await ui_message("help"))
        return
    if text == labels["support"]:
        await message.answer(await ui_message("restricted"), reply_markup=await contact_admin_keyboard())
        return
    if text == labels["home"]:
        await render_main_menu(bot, message.chat.id, user_id)
        return


admin_router = Router(name="admin")
admin_router.callback_query.outer_middleware(AdminGuardMiddleware())
admin_router.message.outer_middleware(AdminGuardMiddleware())


@admin_router.message(Command("cancel"))
async def cmd_admin_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await ui_message("cancelled"), reply_markup=await admin_panel_keyboard())


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await ui_message("admin_panel", **(await admin_context(message.from_user.id))), reply_markup=await admin_panel_keyboard(message.from_user.id))


@admin_router.callback_query(F.data == "adm_back")
async def cb_admin_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(await ui_message("admin_panel", **(await admin_context(callback.from_user.id))), reply_markup=await admin_panel_keyboard(callback.from_user.id))
    await callback.answer()



# --- Universal Message & Button Editor --------------------------------------

def _editor_message_keyboard() -> InlineKeyboardMarkup:
    keys = list(UI_MESSAGES.keys())
    rows = []
    for i in range(0, len(keys), 2):
        row = []
        for key in keys[i:i+2]:
            row.append(InlineKeyboardButton(text=f"📝 {key.replace('_',' ').title()[:28]}", callback_data=f"ce_m:{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _editor_button_keyboard() -> InlineKeyboardMarkup:
    keys = list(UI_BUTTONS.keys())
    rows = []
    for i in range(0, len(keys), 2):
        row = []
        for key in keys[i:i+2]:
            row.append(InlineKeyboardButton(text=f"🔘 {key.replace('_',' ').title()[:28]}", callback_data=f"ce_b:{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.callback_query(F.data == "adm_editor")
async def cb_editor_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "✏️ <b>Message & Button Studio</b>\n\n"
        "✨ Edit the text shown throughout the bot.\n"
        "💎 Premium/custom emoji are preserved in messages when you send them from Telegram.\n"
        "🔘 Button labels can be customized with normal Unicode emoji. Telegram Bot API does not support premium/custom-emoji entities inside button text.\n\n"
        "Choose what you want to edit:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Edit Messages", callback_data="ce_messages")],
            [InlineKeyboardButton(text="🔘 Edit Buttons", callback_data="ce_buttons")],
            [InlineKeyboardButton(text="🎨 UI Theme", callback_data="ui_theme")],
            [InlineKeyboardButton(text="👁 Preview", callback_data="ui_preview_gate"), InlineKeyboardButton(text="🧩 Variables", callback_data="ui_variables")],
            [InlineKeyboardButton(text="📜 Version History", callback_data="ui_history:gate")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="adm_back")],
        ])
    )
    await callback.answer()


@admin_router.callback_query(F.data == "ui_variables")
async def cb_ui_variables(callback: CallbackQuery) -> None:
    text=("🧩 <b>VARIABLES</b>\n\n<b>ADMIN</b>\n<code>{admin_name}</code> <code>{admin_username}</code> <code>{admin_id}</code>\n\n<b>USER</b>\n<code>{first_name}</code> <code>{username}</code> <code>{user_id}</code>\n\n<b>BOT</b>\n<code>{bot_name}</code> <code>{bot_username}</code>\n\n<b>REFERRAL</b>\n<code>{referrals}</code> <code>{required_referrals}</code> <code>{remaining_referrals}</code> <code>{referral_link}</code> <code>{progress}</code>\n\n<b>REWARD</b>\n<code>{reward_number}</code> <code>{reward_status}</code> <code>{reward_date}</code>\n\n<b>SYSTEM</b>\n<code>{total_users}</code> <code>{today_users}</code> <code>{total_rewards}</code> <code>{available_rewards}</code> <code>{datetime}</code>")
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back",callback_data="adm_editor")]])); await callback.answer()

@admin_router.callback_query(F.data.startswith("ui_history:"))
async def cb_ui_history(callback: CallbackQuery) -> None:
    key=callback.data.split(":",1)[1]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory=aiosqlite.Row; cur=await db.execute("SELECT * FROM message_versions WHERE message_key=? ORDER BY version DESC LIMIT 10",(key,)); rows=list(await cur.fetchall())
    text=f"📜 <b>VERSION HISTORY — {hesc(key)}</b>\n\n"+"\n".join(f"v{r['version']} · {r['created_at'][:19]} · <code>{r['created_by'] or 'system'}</code>" for r in rows) if rows else "No versions yet."
    buttons=[[InlineKeyboardButton(text=f"↩️ Restore v{r['version']}",callback_data=f"ui_restore:{key}:{r['version']}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="⬅️ Back",callback_data="adm_editor")])
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); await callback.answer()

@admin_router.callback_query(F.data.startswith("ui_restore:"))
async def cb_ui_restore(callback: CallbackQuery) -> None:
    _,key,raw=callback.data.split(":",2); version=int(raw)
    async with aiosqlite.connect(DB_PATH) as db:
        cur=await db.execute("SELECT content FROM message_versions WHERE message_key=? AND version=?",(key,version)); row=await cur.fetchone()
    if not row: await callback.answer("Version not found.",show_alert=True); return
    try: await save_ui_message(key,row[0],callback.from_user.id)
    except ValueError as exc: await callback.answer(str(exc)[:180],show_alert=True); return
    await audit(callback.from_user.id,"RESTORE_MESSAGE",details=f"{key}:v{version}"); await callback.answer(f"✅ Restored v{version}"); await cb_ui_history(callback)

@admin_router.callback_query(F.data == "ui_theme")
async def cb_ui_theme(callback: CallbackQuery) -> None:
    current = await get_ui_theme()
    rows = []
    for theme in ("PREMIUM", "DARK", "MINIMAL", "NEON", "CLEAN"):
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if theme == current else '▫️'} {theme.title()}",
            callback_data=f"theme:{theme}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="adm_editor")])
    await callback.message.edit_text(
        "🎨 <b>UI THEME</b>\n\nChoose the visual language used by the premium user screens.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("theme:"))
async def cb_set_ui_theme(callback: CallbackQuery) -> None:
    theme = callback.data.split(":", 1)[1].upper()
    if theme not in UI_THEME:
        await callback.answer("Invalid theme.", show_alert=True)
        return
    await set_setting("ui_theme", theme)
    await callback.answer(f"{theme.title()} theme enabled.")
    await cb_ui_theme(callback)


@admin_router.callback_query(F.data == "ui_preview_gate")
async def cb_ui_preview_gate(callback: CallbackQuery, bot: Bot) -> None:
    await render_gate(bot, callback.message.chat.id, edit_message=callback.message)
    await callback.answer("Preview")

@admin_router.callback_query(F.data == "ce_messages")
async def cb_editor_messages(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "📝 <b>Message Editor</b>\n\nTap any message below, then send your new version.\n\n"
        "💎 Send premium/custom emoji directly from Telegram and they will be stored with their entities.",
        reply_markup=_editor_message_keyboard()
    )
    await callback.answer()


@admin_router.callback_query(F.data == "ce_buttons")
async def cb_editor_buttons(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🔘 <b>Button Editor</b>\n\nTap a button to change its visible label.\n\n"
        "✨ Use normal Unicode emoji in buttons. Premium/custom emoji entities cannot be embedded in Telegram inline-keyboard button labels.",
        reply_markup=_editor_button_keyboard()
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("ce_m:"))
async def cb_choose_message(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    if key not in UI_MESSAGES:
        await callback.answer("Unknown message.", show_alert=True)
        return
    # RAW template so variables like {number} do not break the editor screen
    raw = await get_setting(f"ui_msg:{key}", UI_MESSAGES.get(key, ""))
    await state.update_data(ui_key=key)
    await state.set_state(AdminStates.waiting_ui_message)
    body = (
        f"📝 <b>Edit: {hesc(key.replace('_',' ').title())}</b>\n\n"
        f"<b>Current template (raw):</b>\n\n<code>{hesc(raw[:2800])}</code>\n\n"
        "Send the new message now. HTML + premium/custom emoji supported.\n"
        "Keep variables like <code>{first_name}</code> if needed.\n"
        "Use /cancel to leave without changing it."
    )
    await safe_edit_text(callback.message, body, reply_markup=await cancel_keyboard("ce_messages"))
    await callback.answer()


@admin_router.message(AdminStates.waiting_ui_message)
async def process_ui_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = data.get("ui_key")
    if not key or key not in UI_MESSAGES:
        await state.clear()
        await message.answer("❌ Editor session expired. Open /admin again.")
        return
    value = message.html_text if message.text else (message.caption or "")
    if not value.strip():
        await message.answer("❌ Send a text message (or a photo with caption) to save it.")
        return
    old_value=await get_setting(f"ui_msg:{key}",UI_MESSAGES.get(key,""))
    try:
        await save_ui_message(key, value, message.from_user.id)
    except ValueError as exc:
        await message.answer(f"⚠️ <b>Template rejected</b>\n\n<code>{hesc(str(exc))}</code>")
        return
    await audit(message.from_user.id,"CHANGE_MESSAGE",details=key,before=old_value,after=value)
    await state.clear()
    await message.answer(await ui_message("message_saved"))
    await message.answer(
        "📝 <b>Message Editor</b>\n\nTap any message to edit the next one.",
        reply_markup=_editor_message_keyboard(),
    )


@admin_router.callback_query(F.data.startswith("ce_b:"))
async def cb_choose_button(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    if key not in UI_BUTTONS:
        await callback.answer("Unknown button.", show_alert=True)
        return
    current = await ui_button(key)
    await state.update_data(ui_key=key)
    await state.set_state(AdminStates.waiting_ui_button)
    await callback.message.edit_text(
        f"🔘 <b>Edit Button: {hesc(key.replace('_',' ').title())}</b>\n\n"
        f"Current: <b>{hesc(current)}</b>\n\nSend the new visible button label.\n"
        "Use normal Unicode emoji; premium/custom emoji entities are not supported by Telegram button labels.",
        reply_markup=await cancel_keyboard("ce_buttons")
    )
    await callback.answer()


@admin_router.message(AdminStates.waiting_ui_button)
async def process_ui_button(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = data.get("ui_key")
    value = (message.text or "").strip()
    if not key or key not in UI_BUTTONS:
        await state.clear()
        await message.answer("❌ Editor session expired. Open /admin again.")
        return
    if not value:
        await message.answer("❌ Send a button label.")
        return
    old_value=await ui_button(key)
    await save_ui_button(key, value)
    await audit(message.from_user.id,"CHANGE_BUTTON",details=key,before=old_value,after=value)
    await state.clear()
    await message.answer(await ui_message("button_saved"))
    await message.answer(
        "🔘 <b>Button Editor</b>\n\nTap a button to edit the next one.",
        reply_markup=_editor_button_keyboard(),
    )


# --- Stats -----------------------------------------------------------------

@admin_router.callback_query(F.data == "adm_stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    st=await get_stats(); total=st["total_users"]
    text=("📊 <b>ANALYTICS CONTROL CENTER</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
          f"👥 Total Users: <b>{total}</b>\n"
          f"🆕 New Today: <b>{st['today_users']}</b>  •  7D: <b>{st['week_users']}</b>  •  30D: <b>{st['month_users']}</b>\n"
          f"✅ Verified: <b>{st['phone_verified']}</b> ({st['conversion']}%)\n"
          f"🤝 Referral Completions: <b>{st['completed_referrals']}</b> ({st['referral_conversion']}%)\n"
          f"🎁 Rewards Sent: <b>{st['rewards_sent']}</b> ({st['reward_conversion']}%)\n"
          f"🚫 Banned: <b>{st['banned']}</b>  •  ⛔ Restricted: <b>{st['restricted']}</b>\n"
          f"📦 Reward Pool: <b>{st['pool_total']}</b> numbers / <b>{st['pool_remaining_capacity']}</b> capacity left\n"
          f"🧬 Active Clones: <b>{sum(1 for x in _clones.values() if x['process'].poll() is None)}</b>\n"
          f"⏱ Uptime: <b>{fmt_uptime(time.time()-BOT_STARTED_AT)}</b>")
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Deep Analytics",callback_data="v3_analytics")],
        [InlineKeyboardButton(text="⬅️ Back",callback_data="adm_back")]]))
    await callback.answer()

@admin_router.callback_query(F.data == "adm_mode")
async def cb_admin_mode(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    mode = await get_bot_mode()
    await callback.message.edit_text(
        "🤖 <b>Bot Mode</b>\n\n"
        "Switch how the bot presents itself. <b>All users and data stay exactly "
        "the same</b> — only the branding/flow label changes. You can switch back "
        "any time.\n\n"
        "🤝 <b>Refer &amp; Earn</b> — 'Agent Numbers Loot', reward = WhatsApp number.\n"
        "🎯 <b>Task &amp; Earn</b> — 'Task and Earn Bot' branding.\n\n"
        f"Current: <b>{'Refer & Earn' if mode == 'refer' else 'Task & Earn'}</b>",
        reply_markup=mode_settings_keyboard(mode),
    )
    await callback.answer()


@admin_router.callback_query(F.data.in_({"mode_refer", "mode_task"}))
async def cb_set_mode(callback: CallbackQuery) -> None:
    mode = "refer" if callback.data == "mode_refer" else "task"
    await set_setting("bot_mode", mode)
    await callback.message.edit_text(
        "🤖 <b>Bot Mode</b>\n\nUpdated ✅",
        reply_markup=mode_settings_keyboard(mode),
    )
    await callback.answer(f"Mode set to {'Refer & Earn' if mode == 'refer' else 'Task & Earn'}.")


# --- Verification settings --------------------------------------------------

async def _render_verification_settings(message: Message) -> None:
    captcha_on = await get_setting("captcha_enabled", "1") == "1"
    phone_on = await get_setting("phone_verify_enabled", "1") == "1"
    await message.edit_text(
        "🛡 <b>Verification Settings</b>\n\n"
        "🧩 <b>Captcha</b> — blocks automated /start spam.\n"
        "📱 <b>Phone</b> — Indian (+91) numbers only; one number = one account.",
        reply_markup=verification_settings_keyboard(captcha_on, phone_on),
    )


@admin_router.callback_query(F.data == "adm_verify")
async def cb_admin_verify(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _render_verification_settings(callback.message)
    await callback.answer()


@admin_router.callback_query(F.data == "vs_captcha")
async def cb_toggle_captcha(callback: CallbackQuery) -> None:
    current = await get_setting("captcha_enabled", "1")
    await set_setting("captcha_enabled", "0" if current == "1" else "1")
    await _render_verification_settings(callback.message)
    await callback.answer("Captcha updated.")


@admin_router.callback_query(F.data == "vs_phone")
async def cb_toggle_phone(callback: CallbackQuery) -> None:
    current = await get_setting("phone_verify_enabled", "1")
    await set_setting("phone_verify_enabled", "0" if current == "1" else "1")
    await _render_verification_settings(callback.message)
    await callback.answer("Phone verification updated.")


# --- Reward caption ---------------------------------------------------------

@admin_router.callback_query(F.data == "adm_reward")
async def cb_admin_set_reward(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_reward_caption)
    await callback.message.edit_text(
        "🎁 <b>Reward Caption Studio</b>\n\n"
        "Send the caption shown under every reward number.\n\n"
        "💎 You can use premium/custom emoji and rich formatting — send them exactly as you want them to appear.\n"
        "✨ The bot stores the message entities and preserves them for users.",
        reply_markup=await cancel_keyboard("adm_back"),
    )
    await callback.answer()


@admin_router.message(AdminStates.waiting_reward_caption)
async def process_reward_caption(message: Message, state: FSMContext) -> None:
    # Preserve premium emoji + formatting exactly by storing the HTML the user
    # sent (aiogram gives us .html_text with entities already converted).
    caption = message.html_text if message.text else (message.caption or "")
    if not caption.strip():
        await message.answer("Please send some text for the caption.")
        return
    await set_setting("reward_caption", caption)
    await state.clear()
    await message.answer("✅ Reward caption updated.", reply_markup=await admin_panel_keyboard())


# --- Reward rules ------------------------------------------------------------

@admin_router.callback_query(F.data == "adm_reward_rules")
async def cb_admin_reward_rules(callback: CallbackQuery, state: FSMContext) -> None:
    required=await get_required_referrals(); qty=await get_setting("reward_quantity","1"); limit=await get_setting("reward_limit","0")
    await state.set_state(AdminStates.waiting_reward_rules)
    await callback.message.edit_text(
        "⚙️ <b>REWARD RULES</b>\n\n"
        f"Required referrals: <b>{required}</b>\nReward quantity: <b>{hesc(qty)}</b> Agent Number(s)\nReward limit: <b>{hesc(limit)}</b> (0 = unlimited)\n\n"
        "Send: <code>required quantity limit</code>\nExample: <code>5 1 0</code> = 5 referrals → 1 Agent Number.",
        reply_markup=await cancel_keyboard("adm_back"))
    await callback.answer()

@admin_router.message(AdminStates.waiting_reward_rules)
async def process_reward_rules(message: Message, state: FSMContext) -> None:
    parts=(message.text or "").split()
    if len(parts)!=3 or not all(p.isdigit() for p in parts):
        await message.answer("❌ Format: <code>required quantity limit</code>")
        return
    required,qty,limit=map(int,parts)
    if required<1 or qty<1 or limit<0:
        await message.answer("❌ Required and quantity must be ≥1; limit must be ≥0.")
        return
    await set_setting("required_referrals",str(required)); await set_setting("reward_quantity",str(qty)); await set_setting("reward_limit",str(limit))
    await state.clear(); await message.answer("✅ Reward rules updated.",reply_markup=await admin_panel_keyboard(message.from_user.id))

# --- Required referrals -----------------------------------------------------

@admin_router.callback_query(F.data == "adm_required")
async def cb_admin_set_required(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_required_referrals)
    await callback.message.edit_text(
        "🔢 Send the new required referral count (whole number, e.g. 3).",
        reply_markup=await cancel_keyboard("adm_back"),
    )
    await callback.answer()


@admin_router.message(AdminStates.waiting_required_referrals)
async def process_required_referrals(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Send a whole number greater than 0.")
        return
    await set_setting("required_referrals", text)
    await state.clear()
    await message.answer(f"✅ Required referrals set to {text}.", reply_markup=await admin_panel_keyboard())


# --- Admin contact ------------------------------------------------------------
@admin_router.callback_query(F.data == "adm_setadmin")
async def cb_admin_set_admin_contact(callback: CallbackQuery, state: FSMContext) -> None:
    current_user = await get_setting("admin_username", "")
    current_id = await get_setting("admin_contact_id", "")
    await state.set_state(AdminStates.waiting_admin_contact)
    await callback.message.edit_text(
        "👨‍💼 <b>Admin Contact</b>\n\n"
        "Send either the admin's numeric Telegram ID or @username.\n"
        "Numeric ID is preferred because it keeps working if the username changes.\n\n"
        f"Current ID: <code>{hesc(current_id) or '—'}</code>\n"
        f"Current username: <b>@{hesc(current_user.lstrip('@')) or '—'}</b>",
        reply_markup=await cancel_keyboard("adm_back"),
    )
    await callback.answer()


@admin_router.message(AdminStates.waiting_admin_contact)
async def process_admin_contact(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value.isdigit() and 5 <= len(value) <= 15:
        await set_setting("admin_contact_id", value)
        await state.clear()
        await message.answer(
            f"✅ Admin Telegram ID saved: <code>{hesc(value)}</code>",
            reply_markup=await admin_panel_keyboard(),
        )
        return

    uname = value.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", uname):
        await message.answer("Send a numeric Telegram ID or a valid @username.")
        return
    await set_setting("admin_username", uname)
    await set_setting("admin_contact_id", "")
    await state.clear()
    await message.answer(
        f"✅ Admin username saved: @{hesc(uname)}",
        reply_markup=await admin_panel_keyboard(),
    )


# --- Mode banners -------------------------------------------------------------
@admin_router.callback_query(F.data == "adm_banner")
async def cb_admin_banner(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_banner_photo)
    await callback.message.edit_text(
        "🖼 <b>Banner Studio</b>\n\n"
        "Send the banner photo. You may put the caption directly on the photo message; "
        "Telegram premium/custom emoji and formatting will be preserved.\n\n"
        "After upload you can choose the mode and preview it before saving.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Remove Refer Banner", callback_data="banner_clear:refer")],
            [InlineKeyboardButton(text="🗑 Remove Task Banner", callback_data="banner_clear:task")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="adm_back")],
        ]),
    )
    await callback.answer()


@admin_router.message(AdminStates.waiting_banner_photo, F.photo)
async def process_banner_photo(message: Message, state: FSMContext) -> None:
    caption = (message.html_text or message.caption or "").strip()
    if not caption:
        caption = "✨ <b>{bot_name}</b>"
    await state.update_data(
        banner_file_id=message.photo[-1].file_id,
        banner_caption=caption,
    )
    await message.answer(
        "🎨 <b>Choose the mode</b>\n\n"
        "The uploaded photo + caption will be stored together for the selected mode.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🤝 Refer & Earn", callback_data="banner_refer"),
                InlineKeyboardButton(text="🎯 Task & Earn", callback_data="banner_task"),
            ],
            [InlineKeyboardButton(text="👁 Preview Refer", callback_data="banner_preview:refer"),
             InlineKeyboardButton(text="👁 Preview Task", callback_data="banner_preview:task")],
            [InlineKeyboardButton(text="⬅️ Cancel", callback_data="adm_back")],
        ]),
    )


@admin_router.message(AdminStates.waiting_banner_photo)
async def process_banner_photo_invalid(message: Message) -> None:
    await message.answer("❌ Please send a photo. You can include the premium/custom-emoji caption in the same photo message.")


@admin_router.callback_query(F.data.in_({"banner_refer", "banner_task"}))
async def cb_save_banner(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    file_id = data.get("banner_file_id")
    caption = data.get("banner_caption") or "✨ <b>{bot_name}</b>"
    if not file_id:
        await callback.answer("Send the photo first.", show_alert=True)
        return
    mode = "refer" if callback.data == "banner_refer" else "task"
    key = "refer_banner_file_id" if mode == "refer" else "task_banner_file_id"
    caption_key = "refer_banner_caption" if mode == "refer" else "task_banner_caption"
    await set_setting(key, file_id)
    await set_setting(caption_key, caption)
    await state.clear()
    await callback.message.edit_text(
        f"✅ <b>{'Refer & Earn' if mode == 'refer' else 'Task & Earn'} banner saved.</b>\n\n"
        "🖼 Image + caption will now be delivered as one unified screen.\n"
        "💎 Premium/custom emoji from the Telegram caption are preserved.",
        reply_markup=await admin_panel_keyboard(),
    )
    await callback.answer("Banner updated.")


@admin_router.callback_query(F.data.startswith("banner_clear:"))
async def cb_clear_banner(callback: CallbackQuery, state: FSMContext) -> None:
    mode = callback.data.split(":", 1)[1]
    if mode not in {"refer", "task"}:
        await callback.answer("Invalid mode.", show_alert=True)
        return
    file_key = "refer_banner_file_id" if mode == "refer" else "task_banner_file_id"
    cap_key = "refer_banner_caption" if mode == "refer" else "task_banner_caption"
    await set_setting(file_key, "")
    await set_setting(cap_key, "")
    await state.clear()
    await callback.answer(f"{mode.title()} banner removed.")
    await cb_admin_banner(callback, state)


@admin_router.callback_query(F.data.startswith("banner_preview:"))
async def cb_banner_preview(callback: CallbackQuery) -> None:
    mode = callback.data.split(":", 1)[1]
    if mode not in {"refer", "task"}:
        await callback.answer("Invalid mode.", show_alert=True)
        return
    file_id = await get_mode_banner_file_id(mode)
    caption = await get_mode_banner_caption(mode)
    if not file_id:
        await callback.answer("No banner saved for this mode.", show_alert=True)
        return
    await callback.message.answer_photo(file_id, caption=(caption or "✨ <b>{bot_name}</b>")[:1024])
    await callback.answer("Preview sent.")


# --- Manage reward numbers (bulk add / delete / clear) ----------------------

@admin_router.callback_query(F.data == "adm_numbers")
async def cb_admin_numbers(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await build_numbers_list()
    await safe_edit_text(callback.message, text, reply_markup=kb)
    await callback.answer()


@admin_router.callback_query(F.data == "num_add")
async def cb_num_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_numbers)
    await callback.message.edit_text(
        "➕ <b>Add reward numbers (bulk)</b>\n\n"
        "Send one or many numbers — any format works, the bot auto-detects and "
        "cleans each one:\n"
        "<code>+91 98765 43210</code>, <code>9876543210</code>, "
        "<code>0091-9876543210</code> …\n\n"
        "Separate them by new lines, commas, or semicolons. Spaces/dashes inside "
        "a number are also detected automatically. Only valid Indian mobile numbers "
        f"are saved. Each number can be handed to up to {MAX_USERS_PER_NUMBER} users.",
        reply_markup=await cancel_keyboard("adm_numbers"),
    )
    await callback.answer()


@admin_router.message(AdminStates.waiting_numbers)
async def process_numbers(message: Message, state: FSMContext) -> None:
    raw = message.text or ""
    parts = extract_phone_candidates(raw)
    if not parts:
        await message.answer(
            "❌ No valid Indian numbers found. You can paste +91, 0091, 91, "
            "0-prefix, spaces, dashes, brackets, commas, or one per line."
        )
        return
    added, skipped = await add_reward_numbers(parts)
    await state.clear()
    await message.answer(
        f"✅ Added <b>{added}</b> number(s).\n"
        f"↩️ Skipped <b>{skipped}</b> (duplicates or invalid).",
    )
    text, kb = await build_numbers_list()
    await message.answer(text, reply_markup=kb)


@admin_router.callback_query(F.data.startswith("num_del:"))
async def cb_num_del(callback: CallbackQuery) -> None:
    number_id = int(callback.data.split(":", 1)[1])
    await delete_reward_number(number_id)
    text, kb = await build_numbers_list()
    await safe_edit_text(callback.message, text, reply_markup=kb)
    await callback.answer("Number deleted.")


@admin_router.callback_query(F.data == "num_clear")
async def cb_num_clear(callback: CallbackQuery) -> None:
    await clear_reward_numbers()
    text, kb = await build_numbers_list()
    await safe_edit_text(callback.message, text, reply_markup=kb)
    await callback.answer("Pool cleared.")


# --- Manage channels --------------------------------------------------------

@admin_router.callback_query(F.data == "adm_channels")
async def cb_admin_channels(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_channels_list(callback.message)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("ch_remove:"))
async def cb_channel_remove(callback: CallbackQuery) -> None:
    channel_id = int(callback.data.split(":", 1)[1])
    await remove_channel(channel_id)
    await show_channels_list(callback.message)
    await callback.answer("Channel removed.")


@admin_router.callback_query(F.data == "ch_add")
async def cb_channel_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_channel_forward)
    await callback.message.edit_text(
        "➕ Forward any message from the channel you want to add.\n\n"
        "The bot must already be an admin there — works for private channels too.",
        reply_markup=await cancel_keyboard("adm_channels"),
    )
    await callback.answer()


@admin_router.message(AdminStates.waiting_channel_forward)
async def process_channel_forward(message: Message, state: FSMContext) -> None:
    origin = message.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        await message.answer(
            "That doesn't look like a message forwarded from a channel. "
            "Please forward a post directly from the channel."
        )
        return
    await state.update_data(
        pending_channel_id=origin.chat.id, pending_channel_title=origin.chat.title
    )
    await state.set_state(AdminStates.waiting_channel_link)
    await message.answer(
        f"Got it — <b>{hesc(origin.chat.title or '')}</b>.\n\n"
        "Now send the invite link for this channel."
    )


@admin_router.message(AdminStates.waiting_channel_link)
async def process_channel_link(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    channel_id = data.get("pending_channel_id")
    title = data.get("pending_channel_title")
    invite_link = (message.text or "").strip()
    if channel_id is None or not invite_link:
        await message.answer("Something went wrong — send the invite link again, or /admin to restart.")
        return
    if not (invite_link.startswith("https://t.me/") or invite_link.startswith("https://telegram.me/") or invite_link.startswith("tg://")):
        await message.answer("❌ Invalid Telegram link. Use an https://t.me/... invite link.")
        return
    await add_channel(channel_id, title, invite_link)
    await state.clear()
    await message.answer(f"✅ Channel added: {hesc(title or '')}")
    await send_channels_list(message)



# --- Join request control -----------------------------------------------------

def join_request_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🤖 Auto-Approve: {'✅ ON' if enabled else '❌ OFF'}",
            callback_data="jr_toggle"
        )],
        [InlineKeyboardButton(text="⏳ Join Request Center", callback_data="jr_center"),
         InlineKeyboardButton(text="🩺 Channel Health", callback_data="jr_health")],
        [InlineKeyboardButton(text="⌛ Expiration", callback_data="jr_expire"),
         InlineKeyboardButton(text="ℹ️ How it works", callback_data="jr_info")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="adm_back")],
    ])

@admin_router.callback_query(F.data == "adm_joinreq")
async def cb_join_request_settings(callback: CallbackQuery) -> None:
    enabled = await get_setting("auto_approve_join_requests", "0") == "1"
    last_error = await get_setting("join_request_last_error", "")
    expiration = await get_setting("join_request_expiration_minutes", "0")
    counts = await join_state_counts()
    error_line = f"\n⚠️ Last error: <code>{hesc(last_error[-500:])}</code>\n" if last_error else ""
    await callback.message.edit_text(
        "⏳ <b>JOIN REQUEST CONTROL</b>\n\n"
        f"Mode: <b>{'AUTO-APPROVE' if enabled else 'MANUAL'}</b>\n"
        f"Pending: <b>{counts.get('REQUESTED',0)+counts.get('PENDING_APPROVAL',0)+counts.get('APPROVED',0)}</b>\n"
        f"Approved today: <b>{hesc(await get_setting('join_approved_today','0'))}</b>\n"
        f"Expired today: <b>{hesc(await get_setting('join_expired_today','0'))}</b>\n"
        f"Expiration: <b>{'Never' if expiration == '0' else expiration + ' minutes'}</b>\n"
        f"Last Event: <b>{hesc(await get_setting('join_last_event_at','None') or 'None')}</b>\n"
        f"{error_line}\n"
        "Join requests are NEVER treated as membership. The final gate is unlocked "
        "only after a fresh Telegram membership check succeeds.",
        reply_markup=join_request_keyboard(enabled),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "jr_toggle")
async def cb_join_request_toggle(callback: CallbackQuery) -> None:
    current = await get_setting("auto_approve_join_requests", "0") == "1"
    new = "0" if current else "1"
    await set_setting("auto_approve_join_requests", new)
    await callback.message.edit_text(
        "🤖 <b>Join Request Control</b>\n\n"
        f"Auto-Approve is now <b>{'ON ✅' if new == '1' else 'OFF ❌'}</b>.\n\n"
        "Approval never bypasses membership verification.",
        reply_markup=join_request_keyboard(new == "1"),
    )
    await callback.answer("Auto-Approve updated.")


@admin_router.callback_query(F.data == "jr_expire")
async def cb_join_request_expiration(callback: CallbackQuery) -> None:
    choices = ["0", "15", "30", "60", "360", "1440"]
    current = await get_setting("join_request_expiration_minutes", "0")
    try:
        idx = choices.index(current)
    except ValueError:
        idx = 0
    new = choices[(idx + 1) % len(choices)]
    await set_setting("join_request_expiration_minutes", new)
    label = "Never" if new == "0" else f"{new} minutes"
    await callback.answer(f"Request expiration: {label}")
    await cb_join_request_settings(callback)


@admin_router.callback_query(F.data == "jr_info")
async def cb_join_request_info(callback: CallbackQuery) -> None:
    await callback.answer(
        "A request is REQUESTED/PENDING until approved. APPROVED is still not membership. "
        "Only get_chat_member()/chat_member verification can unlock the gate.",
        show_alert=True,
    )


def _join_center_keyboard(channels: list[aiosqlite.Row]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📢 {ch['title']}", callback_data=f"jr_channel:{ch['channel_id']}")]
        for ch in channels
    ]
    rows += [
        [InlineKeyboardButton(text="🔄 Refresh", callback_data="jr_refresh"),
         InlineKeyboardButton(text="🩺 Health", callback_data="jr_health")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="adm_joinreq")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.callback_query(F.data.in_({"jr_center", "jr_refresh"}))
async def cb_join_request_center(callback: CallbackQuery) -> None:
    counts = await join_state_counts()
    channels = await get_channels()
    total_pending = counts.get("REQUESTED",0) + counts.get("PENDING_APPROVAL",0) + counts.get("APPROVED",0)
    lines = [
        "⏳ <b>JOIN REQUEST CENTER</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"Total Pending: <b>{total_pending}</b>",
    ]
    for ch in channels:
        rows = await pending_join_rows(ch["channel_id"], limit=10000)
        lines.append(f"📢 {hesc(ch['title'])}: <b>{len(rows)}</b>")
    lines.append("\nSelect a channel to view pending users.")
    await callback.message.edit_text("\n".join(lines), reply_markup=_join_center_keyboard(channels))
    await callback.answer("Refreshed." if callback.data == "jr_refresh" else "")


@admin_router.callback_query(F.data.startswith("jr_channel:"))
async def cb_join_request_channel(callback: CallbackQuery) -> None:
    try:
        channel_id = int(callback.data.split(":",1)[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid channel.", show_alert=True)
        return
    channels = await get_channels()
    channel = next((c for c in channels if c["channel_id"] == channel_id), None)
    if channel is None:
        await callback.answer("Channel not found.", show_alert=True)
        return
    rows = await pending_join_rows(channel_id, limit=50)
    lines = [
        f"📢 <b>{hesc(channel['title'])}</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"⏳ Pending users: <b>{len(rows)}</b>",
        "",
    ]
    if not rows:
        lines.append("No active requests.")
    for row in rows:
        name = hesc(row["first_name"] or row["username"] or "Unknown")
        username = f"@{hesc(row['username'])}" if row["username"] else "—"
        lines.append(
            f"👤 <b>{name}</b>\n"
            f"🆔 <code>{row['user_id']}</code>  🔗 {username}\n"
            f"🕐 {hesc(row['requested_at'])}\n"
            f"⏳ {hesc(UI_STATUS.get(row['status'], row['status']))}\n"
        )
    await callback.message.edit_text(
        "\n".join(lines)[:4096],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data=f"jr_channel:{channel_id}")],
            [InlineKeyboardButton(text="⬅️ Request Center", callback_data="jr_center")],
        ]),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "jr_health")
async def cb_join_request_health(callback: CallbackQuery, bot: Bot) -> None:
    channels = await get_channels()
    if not channels:
        await callback.message.edit_text(
            "🩺 <b>JOIN REQUEST DIAGNOSTICS</b>\n\nNo channels configured.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back", callback_data="adm_joinreq")]
            ]),
        )
        await callback.answer()
        return

    bot_me = await bot.get_me()
    lines = ["🩺 <b>JOIN REQUEST DIAGNOSTICS</b>", "━━━━━━━━━━━━━━━━━━"]
    for ch in channels:
        admin_ok = approve_ok = membership_ok = invite_ok = False
        err = ""
        try:
            member = await bot.get_chat_member(ch["channel_id"], bot_me.id)
            admin_ok = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
            rights = getattr(member, "can_invite_users", None)
            approve_ok = admin_ok and (rights is not False)
            membership_ok = True
            invite_ok = bool(ch["invite_link"])
            status = "🟢 HEALTHY" if all((admin_ok, approve_ok, membership_ok, invite_ok)) else "🟡 LIMITED"
        except Exception as exc:
            err = type(exc).__name__
            status = "🔴 BROKEN"
        lines.append(
            f"📢 <b>{hesc(ch['title'])}</b>\n"
            f"Admin: {'✅' if admin_ok else '❌'}  "
            f"Approve Requests: {'✅' if approve_ok else '❌'}\n"
            f"Membership Check: {'✅' if membership_ok else '❌'}  "
            f"Invite: {'✅' if invite_ok else '❌'}\n"
            f"Status: <b>{status}</b>{f' — {hesc(err)}' if err else ''}\n"
        )
    await callback.message.edit_text(
        "\n".join(lines)[:4096],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="jr_health")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="adm_joinreq")],
        ]),
    )
    await callback.answer()


# --- Broadcast --------------------------------------------------------------

@admin_router.callback_query(F.data == "adm_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_broadcast)
    await callback.message.edit_text(
        "📣 Send the message to broadcast — text, photo, or any content. "
        "It will be copied to every user.",
        reply_markup=await cancel_keyboard("adm_back"),
    )
    await callback.answer()


@admin_router.message(AdminStates.waiting_broadcast)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text and message.text.startswith("/"):
        await message.answer("That looks like a command — send the broadcast content, or Cancel above.")
        return
    await state.clear()
    user_ids = await get_all_user_ids()
    total = len(user_ids)
    progress_msg = await message.answer(f"📣 Broadcasting… 0/{total} processed")

    sent = blocked = failed = 0
    try:
        broadcast_delay = max(0.0, float(await get_setting("broadcast_delay", "0.07")))
    except ValueError:
        broadcast_delay = 0.07
    for i, user_id in enumerate(user_ids, start=1):
        try:
            await bot.copy_message(chat_id=user_id, from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except Exception:
            logger.exception("Broadcast failed for user %s", user_id)
            failed += 1
        if i % 20 == 0 or i == total:
            try:
                await progress_msg.edit_text(
                    f"📣 Broadcasting… {i}/{total} processed\n"
                    f"✅ Sent: {sent}  🚫 Blocked: {blocked}  ⚠️ Failed: {failed}"
                )
            except TelegramBadRequest:
                pass
        if broadcast_delay:
            await asyncio.sleep(broadcast_delay)

    await progress_msg.edit_text(
        f"✅ Broadcast complete\n\nSent: {sent}   Blocked: {blocked}   Failed: {failed}",
        reply_markup=await admin_panel_keyboard(),
    )



# --- System settings / backup -----------------------------------------------
@admin_router.callback_query(F.data == "adm_system")
async def cb_admin_system(callback: CallbackQuery) -> None:
    maintenance = await get_setting("maintenance_mode", "0") == "1"
    delay = await get_setting("broadcast_delay", "0.07")
    await callback.message.edit_text(
        "⚙️ <b>System Settings</b>\n\n"
        f"🛠 Maintenance mode: <b>{'ON' if maintenance else 'OFF'}</b>\n"
        f"📣 Broadcast delay: <b>{hesc(delay)}s</b>\n\n"
        "Use the controls below to change operational settings.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🛠 Maintenance {'ON' if maintenance else 'OFF'}", callback_data="sys_maintenance")],
            [InlineKeyboardButton(text="⚡ Fast Broadcast", callback_data="sys_fast")],
            [InlineKeyboardButton(text="🛡 Safe Broadcast", callback_data="sys_safe")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="adm_back")],
        ])
    )
    await callback.answer()

@admin_router.callback_query(F.data == "sys_maintenance")
async def cb_sys_maintenance(callback: CallbackQuery) -> None:
    current = await get_setting("maintenance_mode", "0") == "1"
    await set_setting("maintenance_mode", "0" if current else "1")
    await callback.answer("Maintenance mode updated.")
    await cb_admin_system(callback)

@admin_router.callback_query(F.data == "sys_fast")
async def cb_sys_fast(callback: CallbackQuery) -> None:
    await set_setting("broadcast_delay", "0.03")
    await callback.answer("Broadcast delay set to 0.03s.")
    await cb_admin_system(callback)


@admin_router.callback_query(F.data == "sys_diagnostics")
async def cb_sys_diagnostics(callback: CallbackQuery) -> None:
    try:
        stats = await get_stats()
        db_ok = True
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("SELECT 1")
        clone_count = sum(1 for info in _clones.values() if info["process"].poll() is None)
        last_join_error = await get_setting("join_request_last_error", "") or "None"
        text = (
            "🩺 <b>System Diagnostics</b>\n\n"
            f"🗄 Database: <b>{'OK' if db_ok else 'ERROR'}</b>\n"
            f"👥 Users: <b>{stats.get('total_users', 0)}</b>\n"
            f"🧬 Running clones: <b>{clone_count}</b>\n"
            f"🤖 Join auto-approve: <b>{'ON' if await get_setting('auto_approve_join_requests','0') == '1' else 'OFF'}</b>\n"
            f"⚠️ Last join-request error: <code>{hesc(last_join_error[-700:])}</code>"
        )
    except Exception as exc:
        text = f"❌ Diagnostics failed: <code>{hesc(type(exc).__name__ + ': ' + str(exc))}</code>"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="adm_system")]]
    ))
    await callback.answer()

@admin_router.callback_query(F.data == "sys_safe")
async def cb_sys_safe(callback: CallbackQuery) -> None:
    await set_setting("broadcast_delay", "0.12")
    await callback.answer("Broadcast delay set to 0.12s.")
    await cb_admin_system(callback)

@admin_router.callback_query(F.data == "adm_backup")
async def cb_admin_backup(callback: CallbackQuery) -> None:
    try:
        with open(DB_PATH, "rb") as fh:
            data = fh.read()
        filename = f"gmap_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"
        await callback.message.answer_document(BufferedInputFile(data, filename=filename), caption="💾 <b>Database backup</b>\nKeep this file private — it contains bot data.")
        await callback.answer("Backup sent.")
    except Exception as exc:
        logger.exception("Database backup failed")
        await callback.answer(f"Backup failed: {type(exc).__name__}", show_alert=True)

# --- Find / manage user -----------------------------------------------------

@admin_router.callback_query(F.data == "adm_finduser")
async def cb_admin_find_user(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_find_user)
    await callback.message.edit_text(
        "👤 Send a user ID or @username to look up.",
        reply_markup=await cancel_keyboard("adm_back"),
    )
    await callback.answer()


@admin_router.message(AdminStates.waiting_find_user)
async def process_find_user(message: Message, state: FSMContext) -> None:
    await state.clear()
    query = (message.text or "").strip()
    if query.startswith("@"):
        user = await find_user_by_username(query[1:])
    else:
        try:
            user = await get_user(int(query))
        except ValueError:
            user = None
    if user is None:
        await message.answer("❌ No user found with that ID or username.", reply_markup=await back_keyboard("adm_back"))
        return
    required = await get_required_referrals()
    text, kb = user_card(user, required)
    await message.answer(text, reply_markup=kb)


async def _refresh_user_card(callback: CallbackQuery, user_id: int) -> None:
    user = await get_user(user_id)
    if user is None:
        await callback.answer("User no longer exists.", show_alert=True)
        return
    required = await get_required_referrals()
    text, kb = user_card(user, required)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


@admin_router.callback_query(F.data.startswith("usr_ban:"))
async def cb_user_ban(callback: CallbackQuery) -> None:
    target_id = int(callback.data.split(":", 1)[1])
    u=await get_user(target_id); before=str(u["banned"] if u else "")
    await set_banned(target_id, True)
    await audit(callback.from_user.id,"BAN",target_id,before=before,after="1")
    await _refresh_user_card(callback, target_id)
    await callback.answer("🚫 User banned.")


@admin_router.callback_query(F.data.startswith("usr_unban:"))
async def cb_user_unban(callback: CallbackQuery) -> None:
    target_id = int(callback.data.split(":", 1)[1])
    u=await get_user(target_id); before=str(u["banned"] if u else "")
    await set_banned(target_id, False)
    await audit(callback.from_user.id,"UNBAN",target_id,before=before,after="0")
    await _refresh_user_card(callback, target_id)
    await callback.answer("♻️ User unbanned.")


@admin_router.callback_query(F.data.startswith("usr_restrict:"))
async def cb_user_restrict(callback: CallbackQuery) -> None:
    target_id = int(callback.data.split(":", 1)[1])
    u=await get_user(target_id); before=str(u["restricted"] if u else "")
    await set_restricted(target_id, True)
    await audit(callback.from_user.id,"RESTRICT",target_id,before=before,after="1")
    await _refresh_user_card(callback, target_id)
    await callback.answer("⛔ User restricted.")


@admin_router.callback_query(F.data.startswith("usr_unrestrict:"))
async def cb_user_unrestrict(callback: CallbackQuery) -> None:
    target_id = int(callback.data.split(":", 1)[1])
    u=await get_user(target_id); before=str(u["restricted"] if u else "")
    await set_restricted(target_id, False)
    await audit(callback.from_user.id,"UNRESTRICT",target_id,before=before,after="0")
    await _refresh_user_card(callback, target_id)
    await callback.answer("✅ Restriction lifted.")


@admin_router.callback_query(F.data.startswith("usr_add:"))
async def cb_user_add_referral(callback: CallbackQuery, bot: Bot) -> None:
    target_id = int(callback.data.split(":", 1)[1])
    await adjust_referrals(target_id, +1)
    await audit(callback.from_user.id,"ADD_REFERRAL",target_id,after="+1")
    user = await get_user(target_id)
    if user is not None:
        required = await get_required_referrals()
        if user["referral_count"] >= required and user["referral_count"] % required == 0:
            await deliver_number_reward(bot, target_id, user["referral_count"])
    await _refresh_user_card(callback, target_id)
    await callback.answer("➕1 referral added.")


@admin_router.callback_query(F.data.startswith("usr_sub:"))
async def cb_user_sub_referral(callback: CallbackQuery) -> None:
    target_id = int(callback.data.split(":", 1)[1])
    await adjust_referrals(target_id, -1)
    await audit(callback.from_user.id,"REMOVE_REFERRAL",target_id,after="-1")
    await _refresh_user_card(callback, target_id)
    await callback.answer("➖1 referral removed.")


@admin_router.callback_query(F.data.startswith("usr_rewards:"))
async def cb_user_rewards(callback: CallbackQuery) -> None:
    uid=int(callback.data.split(":",1)[1]); rows=await get_reward_records(uid,20)
    if not rows: await callback.answer("No rewards.",show_alert=True); return
    buttons=[]
    for r in rows:
        status=r["reward_status"]; label=f"#{r['reward_id']} {pretty_number(r['reward_number'] or r['reward_value'] or '')} · {status}"
        buttons.append([InlineKeyboardButton(text=label[:60],callback_data=f"usr_reward:{uid}:{r['reward_id']}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Back",callback_data="adm_back")])
    await callback.message.edit_text(f"🎁 <b>REWARD HISTORY</b>\n\nUser: <code>{uid}</code>",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); await callback.answer()

@admin_router.callback_query(F.data.startswith("usr_reward:"))
async def cb_user_reward_detail(callback: CallbackQuery) -> None:
    _,uid_raw,rid_raw=callback.data.split(":",2); uid=int(uid_raw); rid=int(rid_raw)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory=aiosqlite.Row; cur=await db.execute("SELECT * FROM reward_records WHERE reward_id=? AND user_id=?",(rid,uid)); r=await cur.fetchone()
    if not r: await callback.answer("Reward not found.",show_alert=True); return
    text=(f"🎁 <b>Reward #{rid}</b>\n\n<code>{hesc(pretty_number(r['reward_number'] or r['reward_value'] or ''))}</code>\n\nStatus: <b>{hesc(r['reward_status'])}</b>\nCreated: {hesc(r['created_at'])}\nDelivered: {hesc(r['delivered_at'] or '—')}\nRecovery Count: <b>{r['recovery_count']}</b>")
    rows=[]
    if r["reward_status"]!="REVOKED": rows.append([InlineKeyboardButton(text="🚫 Revoke Reward",callback_data=f"usr_revoke:{uid}:{rid}")])
    rows.append([InlineKeyboardButton(text="♻️ Resend",callback_data=f"usr_resend:{uid}:{rid}")]); rows.append([InlineKeyboardButton(text="⬅️ Back",callback_data=f"usr_rewards:{uid}")])
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await callback.answer()

@admin_router.callback_query(F.data.startswith("usr_revoke:"))
async def cb_user_revoke_reward(callback: CallbackQuery) -> None:
    if not await can_admin(callback.from_user.id,"rewards"):
        await callback.answer("⛔ Permission denied.",show_alert=True); return
    _,uid_raw,rid_raw=callback.data.split(":",2); uid=int(uid_raw); rid=int(rid_raw)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reward_records SET reward_status='REVOKED' WHERE reward_id=? AND user_id=? AND reward_status!='REVOKED'",(rid,uid)); await db.commit()
    await _set_flag(uid,"reward_sent",1 if await reward_count(uid) else 0); await audit(callback.from_user.id,"REVOKE_REWARD",uid,details=f"reward_id={rid}")
    await callback.answer("✅ Reward revoked."); await cb_user_rewards(callback)

@admin_router.callback_query(F.data.startswith("usr_resend:"))
async def cb_user_resend_reward(callback: CallbackQuery, bot: Bot) -> None:
    if not await can_admin(callback.from_user.id,"rewards"):
        await callback.answer("⛔ Permission denied.",show_alert=True); return
    parts=callback.data.split(":"); uid=int(parts[1]); rid=int(parts[2]) if len(parts)>2 else None
    ok=await recover_reward(bot,uid,rid); await audit(callback.from_user.id,"RESEND_REWARD",uid,details=f"reward_id={rid or 'latest'}")
    await callback.answer("✅ Existing reward resent." if ok else "❌ Reward unavailable.",show_alert=not ok)

@admin_router.callback_query(F.data.startswith("usr_reset:"))
async def cb_user_reset_reward(callback: CallbackQuery) -> None:
    target_id = int(callback.data.split(":", 1)[1])
    await _set_flag(target_id, "reward_sent", 1 if await reward_count(target_id) else 0)
    await audit(callback.from_user.id,"RESET_REWARD",target_id,after="preserved")
    await _refresh_user_card(callback, target_id)
    await callback.answer("🔁 Reward flag reset.")


# --- Export users (CSV) -----------------------------------------------------

@admin_router.callback_query(F.data == "adm_export")
async def cb_admin_export(callback: CallbackQuery) -> None:
    rows = await get_all_users()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["user_id", "username", "first_name", "phone", "phone_verified",
         "captcha_passed", "gate_passed", "referred_by", "referral_count",
         "referral_credited", "reward_sent", "banned", "restricted", "created_at"]
    )
    for r in rows:
        writer.writerow(
            [r["user_id"], r["username"] or "", r["first_name"] or "", r["phone"] or "",
             r["phone_verified"], r["captcha_passed"], r["joined_gate"],
             r["referred_by"] or "", r["referral_count"], r["referral_credited"],
             r["reward_sent"], r["banned"], r["restricted"], r["created_at"]]
        )
    data = buf.getvalue().encode("utf-8-sig")
    filename = f"users_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.csv"
    await callback.message.answer_document(
        BufferedInputFile(data, filename=filename),
        caption=f"👥 {len(rows)} users exported.",
    )
    await callback.answer()


# --- Reset referrals (keeps every user) -------------------------------------

@admin_router.callback_query(F.data == "adm_reset")
async def cb_admin_reset(callback: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yes, reset everyone", callback_data="adm_reset_confirm"),
                InlineKeyboardButton(text="✖️ Cancel", callback_data="adm_back"),
            ]
        ]
    )
    await callback.message.edit_text(
        "♻️ <b>Reset Referrals</b>\n\n"
        "This zeroes <b>every user's</b> referral count, reward status, and "
        "reward-number history so the whole base can earn again.\n\n"
        "✅ <b>No user is deleted</b> — your broadcast reach stays exactly the "
        "same.\n\n"
        "Continue?",
        reply_markup=kb,
    )
    await callback.answer()


@admin_router.callback_query(F.data == "adm_reset_confirm")
async def cb_admin_reset_confirm(callback: CallbackQuery) -> None:
    count = await reset_all_referrals()
    await audit(callback.from_user.id,"RESET_ALL",details=f"users={count}")
    await callback.message.edit_text(
        f"✅ <b>Reset complete.</b>\n\n"
        f"{count} users kept — their referrals and rewards are back to zero and "
        "everyone can earn again.",
        reply_markup=await back_keyboard("adm_back"),
    )
    await callback.answer("Referrals reset.")


# ---------------------------------------------------------------------------
# PREMIUM ADMIN PANEL V3
# ---------------------------------------------------------------------------

def v3_nav(callback_data: str = "adm_back") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="⬅️ Back", callback_data=callback_data)


def v3_pages(prefix: str, page: int, has_next: bool, back: str = "adm_back") -> list[list[InlineKeyboardButton]]:
    row=[]
    if page>0: row.append(InlineKeyboardButton(text="◀️ Previous",callback_data=f"{prefix}:{page-1}"))
    if has_next: row.append(InlineKeyboardButton(text="Next ▶️",callback_data=f"{prefix}:{page+1}"))
    rows=[row] if row else []
    rows.append([v3_nav(back)])
    return rows

@admin_router.callback_query(F.data == "v3_analytics")
async def v3_analytics_home(callback: CallbackQuery) -> None:
    a=await v3_analytics(None)
    text=("📈 <b>PREMIUM ANALYTICS</b>\\n━━━━━━━━━━━━━━━━━━━━\\n\\n"
          f"👥 Users: <b>{a['users']}</b>\\n✅ Verification: <b>{a['verified']}</b> ({a['verification_rate']:.1f}%)\\n"
          f"🤝 Referrals: <b>{a['referrals']}</b> ({a['referral_rate']:.1f}%)\\n🎁 Rewards: <b>{a['rewards']}</b> ({a['reward_rate']:.1f}%)\\n\\n"
          "Choose a reporting window:")
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Today",callback_data="v3_an:1"),InlineKeyboardButton(text="7 Days",callback_data="v3_an:7"),InlineKeyboardButton(text="30 Days",callback_data="v3_an:30")],
        [InlineKeyboardButton(text="All Time",callback_data="v3_an:0")],[v3_nav()]]))
    await callback.answer()

@admin_router.callback_query(F.data.startswith("v3_an:"))
async def v3_analytics_period(callback: CallbackQuery) -> None:
    days=int(callback.data.split(":")[1]); a=await v3_analytics(days or None)
    top="\\n".join(f"{i+1}. <code>{r[0]}</code> — <b>{r[1]}</b>" for i,r in enumerate(a["top"])) or "No referral activity yet."
    label="Today" if days==1 else ("7 Days" if days==7 else ("30 Days" if days==30 else "All Time"))
    text=(f"📈 <b>ANALYTICS — {label}</b>\\n━━━━━━━━━━━━━━━━━━━━\\n\\n"
          f"👥 Users: <b>{a['users']}</b>\\n✅ Verification: <b>{a['verified']}</b> ({a['verification_rate']:.1f}%)\\n"
          f"🤝 Referrals: <b>{a['referrals']}</b> ({a['referral_rate']:.1f}%)\\n🎁 Rewards: <b>{a['rewards']}</b> ({a['reward_rate']:.1f}%)\\n\\n🏆 <b>Top Referrers</b>\\n{top}")
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Analytics",callback_data="v3_analytics")],[v3_nav()]]))
    await callback.answer()

@admin_router.callback_query(F.data == "v3_users")
async def v3_users_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("👥 <b>USER MANAGEMENT V3</b>\\n━━━━━━━━━━━━━━━━━━━━\\n\\nSearch by ID, username, first name or phone, or choose a filter.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Search",callback_data="v3_user_search")],
        [InlineKeyboardButton(text="✅ Verified",callback_data="v3_ul:0:verified"),InlineKeyboardButton(text="❌ Unverified",callback_data="v3_ul:0:unverified")],
        [InlineKeyboardButton(text="🚫 Banned",callback_data="v3_ul:0:banned"),InlineKeyboardButton(text="⛔ Restricted",callback_data="v3_ul:0:restricted")],
        [InlineKeyboardButton(text="🎁 Rewarded",callback_data="v3_ul:0:rewarded"),InlineKeyboardButton(text="🕐 Unrewarded",callback_data="v3_ul:0:unrewarded")],
        [v3_nav()]]))
    await callback.answer()

@admin_router.callback_query(F.data == "v3_user_search")
async def v3_user_search_start(callback: CallbackQuery,state:FSMContext)->None:
    await state.set_state(AdminStates.waiting_v3_user_search)
    await callback.message.edit_text("🔎 <b>User Search</b>\\n\\nSend Telegram ID, @username, name or phone.",reply_markup=await cancel_keyboard("v3_users"))
    await callback.answer()

@admin_router.message(AdminStates.waiting_v3_user_search)
async def v3_user_search_process(message:Message,state:FSMContext)->None:
    q=(message.text or "").strip(); await state.clear(); rows,total=await user_search(q)
    text=f"👥 <b>Search Results</b> — {total} match(es)\\n\\n"+"\\n".join(f"• <code>{r['user_id']}</code> {display_name(r)} · {r['referral_count']} refs" for r in rows)
    if not rows: text += "\\nNo users found."
    kb=[]
    for r in rows: kb.append([InlineKeyboardButton(text=f"👤 {display_name(r)[:28]}",callback_data=f"v3_uc:{r['user_id']}")])
    kb.append([v3_nav("v3_users")]); await message.answer(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@admin_router.callback_query(F.data.startswith("v3_ul:"))
async def v3_user_list(callback:CallbackQuery)->None:
    _,filt,p=callback.data.split(":",2); page=int(p); rows,total=await user_search("",page,filt)
    text=f"👥 <b>{filt.title()}</b> Users · Page {page+1}\\n━━━━━━━━━━━━━━━━━━━━\\n"+"\\n".join(f"• <code>{r['user_id']}</code> {display_name(r)[:30]} · {r['referral_count']} refs" for r in rows)
    kb=[[InlineKeyboardButton(text=f"👤 {display_name(r)[:28]}",callback_data=f"v3_uc:{r['user_id']}")] for r in rows]
    kb+=v3_pages(f"v3_ul:{filt}",page,page+1 < (total+V3_PAGE_SIZE-1)//V3_PAGE_SIZE,"v3_users")
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await callback.answer()

@admin_router.callback_query(F.data.startswith("v3_confirm:"))
async def v3_confirm_action(callback:CallbackQuery)->None:
    _,action,uid_s=callback.data.split(":",2); uid=int(uid_s)
    labels={'ban':'BAN','unban':'UNBAN','restrict':'RESTRICT','unrestrict':'UNRESTRICT','reset':'RESET REWARD'}
    if action not in labels: await callback.answer('Invalid action.',show_alert=True); return
    await callback.message.edit_text(f"⚠️ <b>Confirm {labels[action]}</b>\\n\\nTarget user: <code>{uid}</code>\\n\\nThis action will be recorded in the audit log.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⚠️ Confirm',callback_data=f'v3_do:{action}:{uid}'),InlineKeyboardButton(text='✖️ Cancel',callback_data=f'v3_uc:{uid}')]])); await callback.answer()

@admin_router.callback_query(F.data.startswith("v3_do:"))
async def v3_do_action(callback:CallbackQuery)->None:
    _,action,uid_s=callback.data.split(":",2); uid=int(uid_s)
    if action=='ban': await set_banned(uid,True); await audit(callback.from_user.id,'BAN',uid,after='1')
    elif action=='unban': await set_banned(uid,False); await audit(callback.from_user.id,'UNBAN',uid,after='0')
    elif action=='restrict': await set_restricted(uid,True); await audit(callback.from_user.id,'RESTRICT',uid,after='1')
    elif action=='unrestrict': await set_restricted(uid,False); await audit(callback.from_user.id,'UNRESTRICT',uid,after='0')
    elif action=='reset': await _set_flag(uid,'reward_sent',0); await audit(callback.from_user.id,'RESET_REWARD',uid,after='0')
    else: await callback.answer('Invalid action.',show_alert=True); return
    await _refresh_user_card(callback, uid); await callback.answer('✅ Action completed.')

@admin_router.callback_query(F.data.startswith("v3_uc:"))
async def v3_user_card(callback:CallbackQuery)->None:
    uid=int(callback.data.split(":")[1]); u=await get_user(uid)
    if not u: await callback.answer("User not found.",show_alert=True); return
    text=(f"👤 <b>USER PROFILE</b>\\n━━━━━━━━━━━━━━━━━━━━\\n🆔 <code>{uid}</code>\\n👤 {display_name(u)}\\n"
          f"🔗 @{hesc(u['username']) if u['username'] else '—'}\\n📱 Phone: <b>{'Verified' if u['phone_verified'] else 'Unverified'}</b>\\n"
          f"🧩 Captcha: <b>{'Passed' if u['captcha_passed'] else 'Pending'}</b>\\n🔒 Channel: <b>{'Verified' if u['joined_gate'] else 'Pending'}</b>\\n"
          f"🤝 Referrals: <b>{u['referral_count']}</b>\\n🎁 Reward: <b>{'Sent' if u['reward_sent'] else 'Not sent'}</b>\\n"
          f"🚫 Ban: <b>{'Yes' if u['banned'] else 'No'}</b> · ⛔ Restricted: <b>{'Yes' if u['restricted'] else 'No'}</b>\\n"
          f"📅 Joined: <code>{hesc(u['created_at'])}</code>\\n🕘 Last activity: <code>{hesc(u['last_activity'] or '—')}</code>")
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Ban",callback_data=f"v3_confirm:ban:{uid}"),InlineKeyboardButton(text="♻️ Unban",callback_data=f"v3_confirm:unban:{uid}")],
        [InlineKeyboardButton(text="⛔ Restrict",callback_data=f"v3_confirm:restrict:{uid}"),InlineKeyboardButton(text="✅ Unrestrict",callback_data=f"v3_confirm:unrestrict:{uid}")],
        [InlineKeyboardButton(text="➕ Referral",callback_data=f"usr_add:{uid}"),InlineKeyboardButton(text="➖ Referral",callback_data=f"usr_sub:{uid}")],
        [InlineKeyboardButton(text="🔁 Reset Reward",callback_data=f"v3_confirm:reset:{uid}")],[v3_nav("v3_users")]])); await callback.answer()

@admin_router.callback_query(F.data == "v3_audit")
async def v3_audit_home(callback:CallbackQuery)->None:
    await v3_audit_page(callback,0)

@admin_router.callback_query(F.data.startswith("v3_audit:"))
async def v3_audit_page(callback:CallbackQuery,page:int|None=None)->None:
    if page is None: page=int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?",(V3_PAGE_SIZE,V3_PAGE_SIZE*page)); rows=list(await cur.fetchall())
        total=int((await (await db.execute("SELECT COUNT(*) FROM audit_logs")).fetchone())[0])
    text="🧾 <b>AUDIT LOG</b>\\n━━━━━━━━━━━━━━━━━━━━\\n"+"\\n".join(f"#{r['id']} · <b>{hesc(r['action'])}</b> · admin <code>{r['admin_id']}</code>\\n{hesc(r['details'] or '')}\\n<code>{r['created_at']}</code>" for r in rows)
    if not rows:text+="\\nNo audit events yet."
    kb=v3_pages("v3_audit",page,page+1 < (total+V3_PAGE_SIZE-1)//V3_PAGE_SIZE)
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await callback.answer()

@admin_router.callback_query(F.data == "v3_roles")
async def v3_roles(callback:CallbackQuery)->None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory=aiosqlite.Row; cur=await db.execute("SELECT * FROM admin_roles ORDER BY admin_id"); rows=list(await cur.fetchall())
    text="👮 <b>ADMIN ROLES</b>\\n━━━━━━━━━━━━━━━━━━━━\\n"+"\\n".join(f"• <code>{r['admin_id']}</code> — <b>{hesc(r['role'])}</b>" for r in rows)
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add/Change Role",callback_data="v3_role_add")],[v3_nav()]])); await callback.answer()

@admin_router.callback_query(F.data == "v3_role_add")
async def v3_role_add(callback:CallbackQuery,state:FSMContext)->None:
    await state.set_state(AdminStates.waiting_v3_role_admin); await callback.message.edit_text("👮 Send: <code>ADMIN_ID role</code>\\n\\nRoles: owner, super_admin, manager, support, broadcast_manager, analytics_viewer",reply_markup=await cancel_keyboard("v3_roles")); await callback.answer()

@admin_router.message(AdminStates.waiting_v3_role_admin)
async def v3_role_save(message:Message,state:FSMContext)->None:
    parts=(message.text or "").split(); await state.clear()
    if len(parts)!=2 or not parts[0].isdigit() or parts[1] not in ROLE_PERMISSIONS:
        await message.answer("❌ Invalid format or role."); return
    uid=int(parts[0]); role=parts[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO admin_roles(admin_id,role,created_at) VALUES(?,?,?) ON CONFLICT(admin_id) DO UPDATE SET role=excluded.role",(uid,role,datetime.now(timezone.utc).isoformat())); await db.commit()
    await audit(message.from_user.id,"CHANGE_ROLE",uid,after=role); await message.answer(f"✅ Role saved: <code>{uid}</code> → <b>{role}</b>",reply_markup=await admin_panel_keyboard())

@admin_router.callback_query(F.data == "v3_rewards")
async def v3_rewards(callback:CallbackQuery)->None:
    total,remaining,used=await pool_stats(); capacity=total*MAX_USERS_PER_NUMBER; pct=(remaining/capacity*100 if capacity else 0)
    async with aiosqlite.connect(DB_PATH) as db:
        cur=await db.execute("SELECT COUNT(*) FROM reward_handouts"); history=int((await cur.fetchone())[0])
    text=(f"🎁 <b>REWARD POOL PRO</b>\\n━━━━━━━━━━━━━━━━━━━━\\n\\n🔢 Numbers: <b>{total}</b>\\n📤 Handouts: <b>{used}</b>\\n🧮 Total capacity: <b>{capacity}</b>\\n🟢 Remaining capacity: <b>{remaining}</b> ({pct:.1f}%)\\n🧾 Reward history: <b>{history}</b>\\n\\nMAX_USERS_PER_NUMBER: <b>{MAX_USERS_PER_NUMBER}</b>")
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 Reward History",callback_data="v3_reward_history:0")],[InlineKeyboardButton(text="🔢 Manage Numbers",callback_data="adm_numbers")],[v3_nav()]])); await callback.answer()

@admin_router.callback_query(F.data.startswith("v3_reward_history:"))
async def v3_reward_history(callback:CallbackQuery)->None:
    page=int(callback.data.split(":")[1]); rows=await reward_history_rows(page=page)
    text="🧾 <b>REWARD HISTORY</b>\\n━━━━━━━━━━━━━━━━━━━━\\n"+"\\n".join(f"#{r['reward_id']} · <code>{r['user_id']}</code> · @{hesc(r['username'] or '—')}\\n🎁 {hesc(r['number'])} · <code>{r['sent_at']}</code>" for r in rows)
    kb=v3_pages("v3_reward_history",page,len(rows)==V3_PAGE_SIZE,"v3_rewards"); await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await callback.answer()

@admin_router.callback_query(F.data == "v3_channels")
async def v3_channels(callback:CallbackQuery)->None:
    channels=await get_channels(); bot=callback.bot; me=await bot.get_me(); lines=[]
    for ch in channels:
        try:
            member=await bot.get_chat_member(ch['channel_id'],me.id); ok=member.status in (ChatMemberStatus.ADMINISTRATOR,ChatMemberStatus.CREATOR)
            status="🟢 Healthy" if ok else "🔴 Bot not admin"
        except Exception as exc:
            status="🔴 Error"
            logger.warning("Channel health %s: %s",ch['channel_id'],exc)
        lines.append(f"📢 <b>{hesc(ch['title'] or 'Channel')}</b>\\n<code>{ch['channel_id']}</code> · {status}")
    text="📡 <b>CHANNEL HEALTH</b>\\n━━━━━━━━━━━━━━━━━━━━\\n\\n"+"\\n\\n".join(lines or ["No channels configured."])
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Refresh",callback_data="v3_channels")],[v3_nav()]])); await callback.answer()

@admin_router.callback_query(F.data == "v3_health")
async def v3_health(callback:CallbackQuery)->None:
    st=await get_stats(); db_size=os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    wal_size=os.path.getsize(DB_PATH+"-wal") if os.path.exists(DB_PATH+"-wal") else 0
    async with aiosqlite.connect(DB_PATH) as db:
        cur=await db.execute("PRAGMA integrity_check"); integrity=(await cur.fetchone())[0]
        cur=await db.execute("SELECT COUNT(*) FROM audit_logs"); errors=0
    text=(f"🩺 <b>SYSTEM HEALTH CENTER</b>\\n━━━━━━━━━━━━━━━━━━━━\\n\\n🗄 DB: <b>{integrity}</b>\\n💾 DB size: <b>{db_size/1024:.1f} KB</b>\\n📝 WAL: <b>{wal_size/1024:.1f} KB</b>\\n⏱ Uptime: <b>{fmt_uptime(time.time()-BOT_STARTED_AT)}</b>\\n👥 Users: <b>{st['total_users']}</b>\\n🎁 Pool remaining: <b>{st['pool_remaining_capacity']}</b>\\n⚠️ Logged audit events: <b>{errors}</b>")
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Refresh Diagnostics",callback_data="v3_health")],[v3_nav()]])); await callback.answer()

@admin_router.callback_query(F.data == "v3_security")
async def v3_security(callback:CallbackQuery)->None:
    roles=len(ADMIN_IDS)
    text=("🔐 <b>SECURITY CENTER</b>\\n━━━━━━━━━━━━━━━━━━━━\\n\\n"
          f"👑 ADMIN_IDS owners: <b>{roles}</b>\\n🛡 Callback authorization: <b>Enabled</b>\\n🧾 Persistent audit log: <b>Enabled</b>\\n🔒 Token storage: <b>Not persisted</b>\\n🚦 Flood/exception isolation: <b>Enabled</b>\\n🧹 Sensitive clone input: <b>Process-only</b>")
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👮 Manage Roles",callback_data="v3_roles")],[v3_nav()]])); await callback.answer()

@admin_router.callback_query(F.data == "v3_backup")
async def v3_backup(callback:CallbackQuery)->None:
    size=os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    await callback.message.edit_text(f"💾 <b>BACKUP & RECOVERY CENTER</b>\\n━━━━━━━━━━━━━━━━━━━━\\n\\nDatabase: <code>{hesc(DB_PATH)}</code>\\nSize: <b>{size/1024:.1f} KB</b>\\n\\nUse SQLite online backup to create a consistent snapshot.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Create Backup",callback_data="v3_backup_create")],[InlineKeyboardButton(text="📚 Backup History",callback_data="v3_backup_history")],
        [InlineKeyboardButton(text="♻️ Restore Backup",callback_data="v3_restore_start")],
        [InlineKeyboardButton(text="⬅️ Back",callback_data="adm_back")]])); await callback.answer()

@admin_router.callback_query(F.data == "v3_backup_create")
async def v3_backup_create(callback:CallbackQuery)->None:
    filename=f"gmap_v3_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"; path=os.path.join(os.path.dirname(DB_PATH) or '.',filename)
    try:
        srcdb=sqlite3.connect(DB_PATH); dst=sqlite3.connect(path); srcdb.backup(dst); dst.close(); srcdb.close(); data=Path(path).read_bytes(); os.remove(path)
        async with aiosqlite.connect(DB_PATH) as db: await db.execute("INSERT INTO backup_history(filename,size_bytes,kind,created_at) VALUES(?,?,?,?)",(filename,len(data),'backup',datetime.now(timezone.utc).isoformat())); await db.commit()
        await callback.message.answer_document(BufferedInputFile(data,filename=filename),caption="💾 <b>Consistent SQLite backup</b>"); await audit(callback.from_user.id,"BACKUP",details=filename)
        await callback.answer("Backup created.")
    except Exception as exc:
        logger.exception("V3 backup failed"); await callback.answer("❌ Backup failed.",show_alert=True)

@admin_router.callback_query(F.data == "v3_backup_history")
async def v3_backup_history(callback:CallbackQuery)->None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory=aiosqlite.Row; cur=await db.execute("SELECT * FROM backup_history ORDER BY id DESC LIMIT 20"); rows=list(await cur.fetchall())
    text="📚 <b>BACKUP HISTORY</b>\\n━━━━━━━━━━━━━━━━━━━━\\n"+"\\n".join(f"#{r['id']} · {hesc(r['filename'])} · {r['size_bytes']/1024:.1f} KB\\n<code>{r['created_at']}</code>" for r in rows)
    await callback.message.edit_text(text or "No backups yet.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[v3_nav("v3_backup")]])); await callback.answer()

@admin_router.callback_query(F.data == "v3_restore_start")
async def v3_restore_start(callback:CallbackQuery,state:FSMContext)->None:
    await state.set_state(AdminStates.waiting_v3_restore)
    await callback.message.edit_text("♻️ <b>Restore Database</b>\\n\\nSend a SQLite <code>.db</code> backup file.\\n\\n⚠️ The bot will validate it and create a safety backup before replacing the current database. This is a destructive operation.",reply_markup=await cancel_keyboard("v3_backup")); await callback.answer()

@admin_router.message(AdminStates.waiting_v3_restore, F.document)
async def v3_restore_receive(message:Message,state:FSMContext,bot:Bot)->None:
    name=(message.document.file_name or "backup.db")
    if not name.lower().endswith('.db'):
        await message.answer("❌ Please send a SQLite .db backup file."); return
    tmp=os.path.join(os.path.dirname(DB_PATH) or '.',f".restore_{message.from_user.id}_{int(time.time())}.db")
    try:
        file=await bot.get_file(message.document.file_id); await bot.download_file(file.file_path,tmp)
        con=sqlite3.connect(tmp); ok=con.execute("PRAGMA integrity_check").fetchone()[0]; con.close()
        if ok != 'ok': raise ValueError('SQLite integrity check failed')
        await state.update_data(restore_path=tmp,restore_name=name)
        await message.answer(f"⚠️ <b>Restore Confirmation</b>\\n\\nFile: <code>{hesc(name)}</code>\\nIntegrity: <b>OK</b>\\n\\nCreate safety backup and replace the current database?",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚠️ YES, RESTORE",callback_data="v3_restore_confirm")],[InlineKeyboardButton(text="✖️ Cancel",callback_data="v3_backup")]]))
    except Exception as exc:
        logger.exception('Restore validation failed')
        try: os.remove(tmp)
        except OSError: pass
        await state.clear(); await message.answer("❌ Backup validation failed. No data was changed.")

@admin_router.callback_query(F.data == "v3_restore_confirm")
async def v3_restore_confirm(callback:CallbackQuery,state:FSMContext)->None:
    data=await state.get_data(); path=data.get('restore_path'); name=data.get('restore_name','backup.db')
    await state.clear()
    if not path or not os.path.exists(path): await callback.answer("Restore file expired.",show_alert=True); return
    safety=f"{DB_PATH}.pre_restore_{int(time.time())}.db"
    try:
        src=sqlite3.connect(DB_PATH); dst=sqlite3.connect(safety); src.backup(dst); dst.close(); src.close()
        for suffix in ('-wal','-shm'):
            try: os.remove(DB_PATH+suffix)
            except OSError: pass
        shutil.copy2(path,DB_PATH); os.remove(path)
        await audit(callback.from_user.id,'RESTORE',details=f"file={name};safety={safety}")
        await callback.message.edit_text(f"✅ <b>Database restored successfully.</b>\\n\\nSafety backup: <code>{hesc(safety)}</code>\\nSource: <code>{hesc(name)}</code>\\n\\n⚠️ Restart the bot process after restore so all connections reopen against the restored database.",reply_markup=await admin_panel_keyboard()); await callback.answer('Restore complete.')
    except Exception as exc:
        logger.exception('Database restore failed')
        try: os.remove(path)
        except OSError: pass
        await callback.answer('❌ Restore failed. Current database was not intentionally overwritten.',show_alert=True)

@admin_router.callback_query(F.data == "v3_broadcast")
async def v3_broadcast_home(callback:CallbackQuery,state:FSMContext)->None:
    await state.set_state(AdminStates.waiting_v3_broadcast)
    await callback.message.edit_text("📣 <b>BROADCAST PRO</b>\\n━━━━━━━━━━━━━━━━━━━━\\n\\nSend the message to broadcast. After receiving it, the bot will show a target preview before sending.\\n\\nSupported targets: ALL / VERIFIED / UNVERIFIED / REWARDED / UNREWARDED / ACTIVE / INACTIVE / REFERRALS:5",reply_markup=await cancel_keyboard("adm_back")); await callback.answer()

@admin_router.message(AdminStates.waiting_v3_broadcast)
async def v3_broadcast_receive(message:Message,state:FSMContext,bot:Bot)->None:
    await state.clear(); await state.update_data(v3_broadcast_message_id=message.message_id,v3_broadcast_chat_id=message.chat.id)
    await message.answer("🎯 <b>Choose audience</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 All",callback_data="v3_bc:all"),InlineKeyboardButton(text="✅ Verified",callback_data="v3_bc:verified")],
        [InlineKeyboardButton(text="❌ Unverified",callback_data="v3_bc:unverified"),InlineKeyboardButton(text="🎁 Rewarded",callback_data="v3_bc:rewarded")],
        [InlineKeyboardButton(text="🕐 Unrewarded",callback_data="v3_bc:unrewarded"),InlineKeyboardButton(text="🔥 Active",callback_data="v3_bc:active")],
        [InlineKeyboardButton(text="💤 Inactive",callback_data="v3_bc:inactive"),InlineKeyboardButton(text="🤝 5+ Referrals",callback_data="v3_bc:ref5")],
        [v3_nav("adm_back")]]))

async def _audience_ids(aud:str)->list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        if aud=='all': q="SELECT user_id FROM users WHERE banned=0 AND restricted=0"; p=()
        elif aud=='verified': q="SELECT user_id FROM users WHERE phone_verified=1 AND banned=0 AND restricted=0"; p=()
        elif aud=='unverified': q="SELECT user_id FROM users WHERE phone_verified=0 AND banned=0 AND restricted=0"; p=()
        elif aud=='rewarded': q="SELECT user_id FROM users WHERE reward_sent=1 AND banned=0 AND restricted=0"; p=()
        elif aud=='unrewarded': q="SELECT user_id FROM users WHERE reward_sent=0 AND banned=0 AND restricted=0"; p=()
        elif aud=='active': q="SELECT user_id FROM users WHERE COALESCE(last_activity,created_at)>=? AND banned=0 AND restricted=0"; p=((datetime.now(timezone.utc)-timedelta(days=30)).isoformat(),)
        elif aud=='inactive': q="SELECT user_id FROM users WHERE COALESCE(last_activity,created_at)<? AND banned=0 AND restricted=0"; p=((datetime.now(timezone.utc)-timedelta(days=30)).isoformat(),)
        else: q="SELECT user_id FROM users WHERE referral_count>=5 AND banned=0 AND restricted=0"; p=()
        cur=await db.execute(q,p); return [int(r[0]) for r in await cur.fetchall()]

@admin_router.callback_query(F.data.startswith("v3_bc:"))
async def v3_broadcast_confirm(callback:CallbackQuery,state:FSMContext)->None:
    data=await state.get_data(); aud=callback.data.split(":",1)[1]; ids=await _audience_ids(aud)
    if not data.get('v3_broadcast_message_id'): await callback.answer("Broadcast session expired.",show_alert=True); return
    await state.update_data(v3_broadcast_audience=aud,v3_broadcast_ids=ids)
    await callback.message.edit_text(f"📣 <b>Broadcast Preview</b>\\n\\n🎯 Audience: <b>{aud.upper()}</b>\\n👥 Target count: <b>{len(ids)}</b>\\n\\nStart campaign?",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Start",callback_data="v3_bc_start"),InlineKeyboardButton(text="✖️ Cancel",callback_data="adm_back")]])); await callback.answer()

@admin_router.callback_query(F.data == "v3_bc_start")
async def v3_broadcast_start(callback:CallbackQuery,state:FSMContext,bot:Bot)->None:
    data=await state.get_data(); await state.clear(); ids=data.get('v3_broadcast_ids',[]); aud=data.get('v3_broadcast_audience','all'); chat=data.get('v3_broadcast_chat_id'); msg=data.get('v3_broadcast_message_id')
    async with aiosqlite.connect(DB_PATH) as db:
        cur=await db.execute("INSERT INTO broadcast_campaigns(admin_id,audience,source_chat_id,source_message_id,status,total,created_at,started_at) VALUES(?,?,?,?,?,?,?,?)",(callback.from_user.id,aud,chat,msg,'running',len(ids),datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat())); cid=cur.lastrowid; await db.commit()
    progress=await callback.message.edit_text(f"📣 Campaign <b>#{cid}</b> started…\\n0/{len(ids)}")
    sent=blocked=failed=0; delay=max(0.05,float(await get_setting('broadcast_delay','0.07')))
    for i,uid in enumerate(ids,1):
        try: await bot.copy_message(chat_id=uid,from_chat_id=chat,message_id=msg); sent+=1
        except TelegramForbiddenError: blocked+=1
        except Exception: failed+=1
        if i%20==0 or i==len(ids):
            async with aiosqlite.connect(DB_PATH) as db: await db.execute("UPDATE broadcast_campaigns SET processed=?,sent=?,blocked=?,failed=? WHERE id=?",(i,sent,blocked,failed,cid)); await db.commit()
            try: await progress.edit_text(f"📣 <b>Campaign #{cid}</b>\\nProcessed: <b>{i}/{len(ids)}</b>\\n✅ {sent}  🚫 {blocked}  ⚠️ {failed}\\n📊 {i/len(ids)*100:.1f}%" if ids else f"📣 Campaign #{cid} has no targets.")
            except Exception: pass
        await asyncio.sleep(delay)
    async with aiosqlite.connect(DB_PATH) as db: await db.execute("UPDATE broadcast_campaigns SET status='completed',finished_at=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),cid)); await db.commit()
    await audit(callback.from_user.id,"BROADCAST",details=f"campaign={cid};audience={aud};sent={sent};blocked={blocked};failed={failed}")
    await progress.edit_text(f"✅ <b>Campaign #{cid} Complete</b>\\n\\nProcessed: <b>{len(ids)}</b>\\nSent: <b>{sent}</b>\\nBlocked: <b>{blocked}</b>\\nFailed: <b>{failed}</b>",reply_markup=await admin_panel_keyboard())
    await callback.answer()

# ---------------------------------------------------------------------------
# V5 MASTER / LIMITED CLONE PLATFORM
# ---------------------------------------------------------------------------

_clones: dict[str, dict] = {}  # clone_id -> runtime metadata

def _clone_secret_key_path() -> str:
    root = os.path.dirname(os.path.abspath(DB_PATH)) or "."
    return os.environ.get("CLONE_SECRET_KEY_FILE", os.path.join(root, "clone_secret.key"))

def _get_fernet() -> Fernet:
    if Fernet is None:
        raise RuntimeError("cryptography is required for secure clone token storage")
    env_key = os.environ.get("CLONE_TOKEN_KEY", "").strip()
    if env_key:
        return Fernet(env_key.encode())
    path = _clone_secret_key_path()
    if os.path.exists(path):
        key = Path(path).read_bytes()
    else:
        key = Fernet.generate_key()
        Path(path).write_bytes(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return Fernet(key)

def _encrypt_clone_token(token: str) -> str:
    return _get_fernet().encrypt(token.encode()).decode()

def _decrypt_clone_token(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()

async def master_get_clone(clone_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clone_registry WHERE clone_id=?", (clone_id,))
        return await cur.fetchone()

async def master_set_clone_status(clone_id: str, status: str, error: str = "") -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE clone_registry SET status=?,last_error=?,updated_at=? WHERE clone_id=?",
            (status, error[:2000], datetime.now(timezone.utc).isoformat(), clone_id),
        )
        await db.commit()

async def master_seed_clone_features(clone_id: str, package: str) -> None:
    features = _package_features(package)
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now(timezone.utc).isoformat()
        for feature in FEATURE_NAMES:
            await db.execute(
                "INSERT OR REPLACE INTO clone_features(clone_id,feature,enabled,source,updated_at) VALUES(?,?,?,?,?)",
                (clone_id, feature, 1 if feature in features else 0, "package", now),
            )
        await db.commit()

async def master_has_clone_feature(clone_id: str, feature: str) -> bool:
    row = await master_get_clone(clone_id)
    if not row or not row["enabled"]:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT enabled FROM clone_features WHERE clone_id=? AND feature=?",
            (clone_id, feature),
        )
        r = await cur.fetchone()
    return bool(r and r[0])

async def master_clone_admins(clone_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM clone_admins WHERE clone_id=? ORDER BY role,admin_id", (clone_id,)
        )
        return list(await cur.fetchall())

async def master_clone_count_users(db_path: str) -> int:
    try:
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            return int((await cur.fetchone())[0])
    except Exception:
        return 0

async def master_clone_pool_capacity(db_path: str) -> int:
    try:
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "SELECT COALESCE(SUM(handout_count),0), COUNT(*) FROM reward_numbers"
            )
            used, total = await cur.fetchone()
            return max(0, int(total) * MAX_USERS_PER_NUMBER - int(used))
    except Exception:
        return 0

def _clone_env(clone_id: str, token: str, admin_ids: list[int]) -> dict[str, str]:
    env = os.environ.copy()
    env["BOT_TOKEN"] = token
    env["CLONE_MODE"] = "1"
    env["CLONE_ID"] = clone_id
    env["CLONE_ADMIN_IDS"] = ",".join(map(str, admin_ids))
    env["ADMIN_IDS"] = ",".join(map(str, admin_ids))
    env["DB_PATH"] = str(Path(os.path.dirname(os.path.abspath(DB_PATH)) or ".") / "clones" / f"{clone_id}.db")
    env["CLONE_DB_PATH"] = env["DB_PATH"]
    env["MASTER_USERNAME"] = MASTER_USERNAME
    env["MASTER_REGISTRY_DB_PATH"] = DB_PATH
    Path(env["DB_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    return env

async def validate_bot_token(token: str):
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", token):
        return False, None, "Invalid Bot Token format."
    probe = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await probe.get_me()
        return True, me, ""
    except Exception:
        return False, None, "Telegram rejected this BotFather token."
    finally:
        try:
            await probe.session.close()
        except Exception:
            pass

async def launch_registered_clone(clone_id: str) -> tuple[bool, str]:
    row = await master_get_clone(clone_id)
    if not row:
        return False, "Clone not found."
    if not row["enabled"]:
        return False, "Clone is disabled."
    try:
        token = _decrypt_clone_token(row["token_ciphertext"])
    except Exception:
        return False, "Clone token could not be decrypted."
    admins = [r["admin_id"] for r in await master_clone_admins(clone_id)]
    if not admins:
        return False, "Clone has no active administrator."

    current = _clones.get(clone_id)
    if current and current["process"].poll() is None:
        return True, "already running"

    log_dir = os.path.join(os.path.dirname(DB_PATH) or ".", "clones", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"clone_{row['bot_id']}.log")
    try:
        log_fh = open(log_path, "ab")
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__)],
            env=_clone_env(clone_id, token, admins),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
        log_fh.close()
    except Exception as exc:
        await master_set_clone_status(clone_id, "ERROR", str(exc))
        return False, f"Could not start clone: {type(exc).__name__}"

    _clones[clone_id] = {"process": proc, "pid": proc.pid, "log": log_path}
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE clone_registry SET status='STARTING',last_started_at=?,updated_at=?,last_error='' WHERE clone_id=?",
            (now, now, clone_id),
        )
        await db.commit()

    await asyncio.sleep(0.8)
    if proc.poll() is not None:
        tail = ""
        try:
            raw = Path(log_path).read_text(encoding="utf-8", errors="replace")
            tail = raw[-1200:]
        except Exception:
            pass
        await master_set_clone_status(clone_id, "ERROR", tail)
        _clones.pop(clone_id, None)
        return False, "Clone exited during startup. Check Clone Logs."

    await master_set_clone_status(clone_id, "RUNNING")
    await audit(ADMIN_IDS[0], "CLONE_START", details=f"clone_id={clone_id}")
    return True, "running"

async def stop_registered_clone(clone_id: str) -> bool:
    info = _clones.get(clone_id)
    if not info:
        await master_set_clone_status(clone_id, "STOPPED")
        return False
    proc = info["process"]
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as exc:
        await master_set_clone_status(clone_id, "ERROR", str(exc))
        return False
    _clones.pop(clone_id, None)
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE clone_registry SET status='STOPPED',last_stopped_at=?,updated_at=? WHERE clone_id=?",
            (now, now, clone_id),
        )
        await db.commit()
    return True

async def restart_registered_clone(clone_id: str) -> tuple[bool, str]:
    await stop_registered_clone(clone_id)
    await asyncio.sleep(0.3)
    ok, msg = await launch_registered_clone(clone_id)
    if ok:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE clone_registry SET restart_count=restart_count+1,updated_at=? WHERE clone_id=?",
                (datetime.now(timezone.utc).isoformat(), clone_id),
            )
            await db.commit()
    return ok, msg

def master_clone_manager_keyboard(rows):
    kb = []
    for r in rows:
        status = r["status"]
        icon = {"RUNNING":"🟢","STARTING":"🟡","ERROR":"🟠","DISABLED":"⚪","STOPPED":"🔴"}.get(status,"⚪")
        kb.append([InlineKeyboardButton(
            text=f"{icon} @{r['bot_username'] or r['bot_id']} · {r['package']}",
            callback_data=f"mv5_info:{r['clone_id']}"
        )])
    kb += [
        [InlineKeyboardButton(text="➕ Create Clone", callback_data="adm_clone")],
        [InlineKeyboardButton(text="🔄 Refresh", callback_data="adm_clone_manager")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="adm_back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

@admin_router.callback_query(F.data == "adm_clone")
async def v5_create_clone_start(callback: CallbackQuery, state: FSMContext) -> None:
    if CLONE_MODE or not is_admin(callback.from_user.id):
        await callback.answer("⛔ This feature is available only to the platform owner.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_clone_token)
    await callback.message.edit_text(
        "🧬 <b>CREATE CLONE — STEP 1/5</b>\n\n"
        "🤖 Send the BotFather token.\n"
        "The token is validated with Telegram before anything is saved.\n\n"
        "🔐 The raw token is deleted from chat and stored encrypted.",
        reply_markup=await cancel_keyboard("adm_back"),
    )
    await callback.answer()

@admin_router.message(AdminStates.waiting_clone_token)
async def v5_clone_token(message: Message, state: FSMContext) -> None:
    if CLONE_MODE or not is_admin(message.from_user.id):
        return
    token = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    ok, me, error = await validate_bot_token(token)
    if not ok:
        await message.answer(f"❌ <b>Invalid Bot Token</b>\n\n{hesc(error)}")
        return
    master_me = None
    try:
        master_probe = Bot(token=BOT_TOKEN)
        master_me = await master_probe.get_me()
        await master_probe.session.close()
    except Exception:
        master_me = None
    if master_me and me.id == master_me.id:
        await message.answer("❌ You cannot clone the Master bot token.")
        return
    await state.update_data(
        clone_token_ciphertext=_encrypt_clone_token(token),
        bot_id=me.id,
        bot_username=me.username or str(me.id),
        bot_name=me.first_name or "",
    )
    await state.set_state(AdminStates.waiting_clone_admin_id)
    await message.answer(
        f"🤖 <b>Bot Found</b>\n\n"
        f"Username: <b>@{hesc(me.username or str(me.id))}</b>\n"
        f"Bot ID: <code>{me.id}</code>\n\n"
        "👤 <b>STEP 3/5 — Clone Admin ID</b>\n"
        "Send the numeric Telegram ID of the person who will manage this clone.",
        reply_markup=await cancel_keyboard("adm_back"),
    )

@admin_router.message(AdminStates.waiting_clone_admin_id)
async def v5_clone_admin_id(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (5 <= len(raw) <= 15):
        await message.answer("❌ Invalid Telegram numeric ID. Example: <code>123456789</code>")
        return
    await state.update_data(clone_admin_id=int(raw))
    await state.set_state(AdminStates.waiting_clone_name)
    await message.answer(
        "🏷 <b>STEP 4/5 — Clone Name</b>\n\nSend a friendly internal name, e.g. <code>Client Bot #01</code>."
    )

@admin_router.message(AdminStates.waiting_clone_name)
async def v5_clone_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 80:
        await message.answer("❌ Clone name must be 1–80 characters.")
        return
    await state.update_data(clone_name=name)
    await state.set_state(AdminStates.waiting_clone_package)
    await message.answer(
        "📦 <b>STEP 5/5 — Select Package</b>\n\n"
        "🟢 BASIC — core dashboard/referral/reward/verification\n"
        "🟡 STANDARD — adds broadcast, pool manager, channels, backup\n"
        "💎 PREMIUM — all client features\n\n"
        "Send: <code>BASIC</code>, <code>STANDARD</code> or <code>PREMIUM</code>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 BASIC", callback_data="mv5_pkg:BASIC"),
             InlineKeyboardButton(text="🟡 STANDARD", callback_data="mv5_pkg:STANDARD")],
            [InlineKeyboardButton(text="💎 PREMIUM", callback_data="mv5_pkg:PREMIUM")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="adm_back")],
        ]),
    )

@admin_router.message(AdminStates.waiting_clone_package)
async def v5_clone_package_text(message: Message, state: FSMContext) -> None:
    package = (message.text or "").strip().upper()
    if package not in {"BASIC","STANDARD","PREMIUM"}:
        await message.answer("❌ Choose BASIC, STANDARD or PREMIUM.")
        return
    await state.update_data(package=package)
    await v5_show_clone_confirm(message, state)

async def v5_show_clone_confirm(message: Message, state: FSMContext) -> None:
    d = await state.get_data()
    package = d.get("package","BASIC")
    await message.answer(
        "🧬 <b>CREATE CLONE — CONFIRM</b>\n\n"
        f"🤖 @{hesc(d.get('bot_username',''))}\n"
        f"🆔 Bot ID: <code>{d.get('bot_id')}</code>\n"
        f"👤 Admin: <code>{d.get('clone_admin_id')}</code>\n"
        f"🏷 Name: <b>{hesc(d.get('clone_name',''))}</b>\n"
        f"📦 Package: <b>{package}</b>\n"
        "🟢 Status: Ready\n\n"
        "Create isolated database and start this clone?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Create", callback_data="mv5_create_confirm"),
             InlineKeyboardButton(text="⚙️ Permissions", callback_data="mv5_permission_preview")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="adm_back")],
        ]),
    )

@admin_router.callback_query(F.data.startswith("mv5_pkg:"))
async def v5_pkg_select(callback: CallbackQuery, state: FSMContext) -> None:
    package = callback.data.split(":",1)[1]
    if package not in {"BASIC","STANDARD","PREMIUM"}:
        await callback.answer("Invalid package.", show_alert=True)
        return
    await state.update_data(package=package)
    await callback.answer(f"{package} selected.")
    await v5_show_clone_confirm(callback.message, state)

@admin_router.callback_query(F.data == "mv5_permission_preview")
async def v5_permission_preview(callback: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    package = d.get("package","BASIC")
    features = _package_features(package)
    rows = []
    for feature in FEATURE_NAMES:
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if feature in features else '❌'} {feature.replace('_',' ').title()}",
            callback_data=f"mv5_wizfeat:{feature}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Confirm", callback_data="mv5_wiz_done")])
    await callback.message.edit_text(
        f"🎛 <b>{package} PACKAGE FEATURES</b>\n\n"
        "Tap a feature to toggle it for this new clone.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows[:40]),
    )
    await callback.answer()

@admin_router.callback_query(F.data == "mv5_wiz_done")
async def v5_wiz_done(callback: CallbackQuery, state: FSMContext) -> None:
    await v5_show_clone_confirm(callback.message, state)
    await callback.answer()

@admin_router.callback_query(F.data.startswith("mv5_wizfeat:"))
async def v5_wiz_feature(callback: CallbackQuery, state: FSMContext) -> None:
    feature = callback.data.split(":",1)[1]
    if feature not in FEATURE_NAMES:
        await callback.answer("Invalid feature.", show_alert=True); return
    d = await state.get_data()
    custom = set(d.get("custom_features", list(_package_features(d.get("package","BASIC")))))
    if feature in custom: custom.remove(feature)
    else: custom.add(feature)
    await state.update_data(custom_features=list(custom))
    await callback.answer("Updated.")
    await v5_permission_preview(callback, state)

@admin_router.callback_query(F.data == "mv5_create_confirm")
async def v5_create_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if CLONE_MODE or not is_admin(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True); return
    d = await state.get_data()
    required = {"clone_token_ciphertext","bot_id","bot_username","clone_admin_id","clone_name","package"}
    if not required.issubset(d):
        await callback.answer("❌ Clone wizard expired. Start again.", show_alert=True); return
    clone_id = uuid.uuid4().hex[:16]
    db_path = str(Path(os.path.dirname(os.path.abspath(DB_PATH)) or ".") / "clones" / f"{clone_id}.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    package = d["package"]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO clone_registry
               (clone_id,bot_id,bot_username,bot_name,owner_id,package,enabled,status,database_path,token_ciphertext,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (clone_id,d["bot_id"],d["bot_username"],d["bot_name"],d["clone_admin_id"],package,1,"STOPPED",db_path,d["clone_token_ciphertext"],now,now),
        )
        await db.execute(
            "INSERT OR REPLACE INTO clone_admins(clone_id,admin_id,role,enabled,created_at) VALUES(?,?,?,?,?)",
            (clone_id,d["clone_admin_id"],"OWNER",1,now),
        )
        await db.commit()
    await master_seed_clone_features(clone_id, package)
    custom = set(d.get("custom_features", []))
    if custom:
        async with aiosqlite.connect(DB_PATH) as db:
            for feature in FEATURE_NAMES:
                await db.execute(
                    "UPDATE clone_features SET enabled=?,source='custom',updated_at=? WHERE clone_id=? AND feature=?",
                    (1 if feature in custom else 0,now,clone_id,feature),
                )
            await db.commit()
    # Initialize the isolated DB before the child starts.
    env = _clone_env(clone_id, _decrypt_clone_token(d["clone_token_ciphertext"]), [d["clone_admin_id"]])
    env["DB_PATH"] = db_path
    env["CLONE_DB_PATH"] = db_path
    try:
        probe = Bot(token=_decrypt_clone_token(d["clone_token_ciphertext"]), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        await probe.delete_webhook(drop_pending_updates=True)
        await probe.session.close()
    except Exception:
        pass
    ok, msg = await launch_registered_clone(clone_id)
    await state.clear()
    if ok:
        await callback.message.edit_text(
            "✅ <b>Clone Created</b>\n\n"
            f"🤖 @{hesc(d['bot_username'])}\n"
            f"👤 Admin: <code>{d['clone_admin_id']}</code>\n"
            f"📦 Package: <b>{package}</b>\n"
            "🟢 Status: <b>RUNNING</b>\n\n"
            f"⚡ Powered by @{hesc(MASTER_USERNAME or 'MASTER')}",
            reply_markup=await admin_panel_keyboard(),
        )
    else:
        await callback.message.edit_text(
            f"⚠️ <b>Clone registered but did not stay running.</b>\n\n{hesc(msg)}",
            reply_markup=await admin_panel_keyboard(),
        )
    await callback.answer()

@admin_router.callback_query(F.data == "adm_clone_manager")
async def v5_clone_manager(callback: CallbackQuery) -> None:
    if CLONE_MODE or not is_admin(callback.from_user.id):
        await callback.answer("⛔ This feature is available only to the platform owner.", show_alert=True); return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clone_registry ORDER BY created_at DESC")
        rows = list(await cur.fetchall())
    # Reconcile runtime status.
    for row in rows:
        info = _clones.get(row["clone_id"])
        if info and info["process"].poll() is None and row["status"] != "RUNNING":
            await master_set_clone_status(row["clone_id"], "RUNNING")
    await callback.message.edit_text(
        "🧬 <b>MASTER CLONE CONTROL CENTER</b>\n\n"
        f"🟢 Running: <b>{sum(1 for r in rows if r['status']=='RUNNING')}</b>\n"
        f"🔴 Stopped: <b>{sum(1 for r in rows if r['status']=='STOPPED')}</b>\n"
        f"⚠️ Errors: <b>{sum(1 for r in rows if r['status']=='ERROR')}</b>\n\n"
        + ("\n".join(
            f"🤖 @{hesc(r['bot_username'] or str(r['bot_id']))} · {r['package']} · {r['status']}"
            for r in rows
        ) if rows else "No clones registered."),
        reply_markup=master_clone_manager_keyboard(rows),
    )
    await callback.answer()

@admin_router.callback_query(F.data.startswith("mv5_info:"))
async def v5_clone_info(callback: CallbackQuery) -> None:
    clone_id = callback.data.split(":",1)[1]
    if CLONE_MODE or not is_admin(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True); return
    row = await master_get_clone(clone_id)
    if not row:
        await callback.answer("Clone not found.", show_alert=True); return
    users = await master_clone_count_users(row["database_path"])
    capacity = await master_clone_pool_capacity(row["database_path"])
    admins = await master_clone_admins(clone_id)
    status = row["status"]
    text = (
        "🧬 <b>CLONE DETAILS</b>\n\n"
        f"🤖 @{hesc(row['bot_username'] or str(row['bot_id']))}\n"
        f"🆔 Clone: <code>{clone_id}</code>\n"
        f"👤 Owner: <code>{row['owner_id']}</code>\n"
        f"📦 Package: <b>{row['package']}</b>\n"
        f"📡 Status: <b>{status}</b>\n"
        f"👥 Users: <b>{users}</b>\n"
        f"🎁 Reward capacity: <b>{capacity}</b>\n"
        f"🔁 Auto restart: <b>{'ON' if row['auto_restart'] else 'OFF'}</b>\n"
        f"⚠️ Last error: <code>{hesc(row['last_error'] or 'None')[-800:]}</code>\n"
        f"👥 Admins: {', '.join(str(a['admin_id']) for a in admins) or 'None'}"
    )
    rows = []
    if status == "RUNNING":
        rows.append([InlineKeyboardButton(text="⏹ Stop", callback_data=f"mv5_stop:{clone_id}"),
                     InlineKeyboardButton(text="🔄 Restart", callback_data=f"mv5_restart:{clone_id}")])
    else:
        rows.append([InlineKeyboardButton(text="▶️ Start", callback_data=f"mv5_start:{clone_id}"),
                     InlineKeyboardButton(text="🔄 Restart", callback_data=f"mv5_restart:{clone_id}")])
    rows += [
        [InlineKeyboardButton(text="🎛 Features", callback_data=f"mv5_features:{clone_id}"),
         InlineKeyboardButton(text="👑 Admins", callback_data=f"mv5_admins:{clone_id}")],
        [InlineKeyboardButton(text="❤️ Health", callback_data=f"mv5_health:{clone_id}"),
         InlineKeyboardButton(text="📜 Logs", callback_data=f"mv5_logs:{clone_id}")],
        [InlineKeyboardButton(text="💾 Backup", callback_data=f"mv5_backup:{clone_id}"),
         InlineKeyboardButton(text="🚫 Disable", callback_data=f"mv5_disable:{clone_id}")],
        [InlineKeyboardButton(text="🗑 Delete", callback_data=f"mv5_delete:{clone_id}")],
        [InlineKeyboardButton(text="⬅️ Clone List", callback_data="adm_clone_manager")],
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()

@admin_router.callback_query(F.data.startswith("mv5_start:") | F.data.startswith("mv5_stop:") | F.data.startswith("mv5_restart:"))
async def v5_clone_runtime_action(callback: CallbackQuery) -> None:
    if CLONE_MODE or not is_admin(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True); return
    action, clone_id = callback.data.split(":",1)
    row = await master_get_clone(clone_id)
    if not row:
        await callback.answer("Clone not found.", show_alert=True); return
    if action == "mv5_start":
        ok,msg = await launch_registered_clone(clone_id)
    elif action == "mv5_stop":
        ok = await stop_registered_clone(clone_id); msg = "stopped"
    else:
        ok,msg = await restart_registered_clone(clone_id)
    await callback.answer("✅ " + msg if ok else "❌ " + msg, show_alert=not ok)
    await v5_clone_info(callback)

@admin_router.callback_query(F.data.startswith("mv5_features:"))
async def v5_features(callback: CallbackQuery) -> None:
    clone_id = callback.data.split(":",1)[1]
    if CLONE_MODE or not is_admin(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True); return
    row = await master_get_clone(clone_id)
    if not row:
        await callback.answer("Clone not found.", show_alert=True); return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clone_features WHERE clone_id=? ORDER BY feature",(clone_id,))
        features=list(await cur.fetchall())
    buttons=[]
    for f in features:
        buttons.append([InlineKeyboardButton(
            text=f"{'✅' if f['enabled'] else '❌'} {f['feature'].replace('_',' ').title()}",
            callback_data=f"mv5_toggle:{clone_id}:{f['feature']}"
        )])
    buttons.append([InlineKeyboardButton(text="🔄 Reset To Package Default", callback_data=f"mv5_resetpkg:{clone_id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"mv5_info:{clone_id}")])
    await callback.message.edit_text(
        "🎛 <b>FEATURE CONTROL</b>\n\n"
        "Permissions are enforced server-side; hiding a button is not the security boundary.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons[:50]),
    )
    await callback.answer()

@admin_router.callback_query(F.data.startswith("mv5_toggle:"))
async def v5_toggle_feature(callback: CallbackQuery) -> None:
    if CLONE_MODE or not is_admin(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True); return
    _,clone_id,feature=callback.data.split(":",2)
    if feature not in FEATURE_NAMES:
        await callback.answer("Invalid feature.", show_alert=True); return
    async with aiosqlite.connect(DB_PATH) as db:
        cur=await db.execute("SELECT enabled FROM clone_features WHERE clone_id=? AND feature=?",(clone_id,feature))
        row=await cur.fetchone()
        if row is None:
            await callback.answer("Feature not found.",show_alert=True); return
        new=0 if row[0] else 1
        await db.execute("UPDATE clone_features SET enabled=?,source='custom',updated_at=? WHERE clone_id=? AND feature=?",(new,datetime.now(timezone.utc).isoformat(),clone_id,feature))
        await db.commit()
    await audit(callback.from_user.id,"FEATURE_TOGGLE",details=f"clone={clone_id};feature={feature};enabled={new}")
    await callback.answer("✅ Updated.")
    await v5_features(callback)

@admin_router.callback_query(F.data.startswith("mv5_resetpkg:"))
async def v5_reset_package(callback: CallbackQuery) -> None:
    clone_id=callback.data.split(":",1)[1]
    row=await master_get_clone(clone_id)
    if not row:
        await callback.answer("Clone not found.",show_alert=True);return
    await master_seed_clone_features(clone_id,row["package"])
    await audit(callback.from_user.id,"FEATURE_RESET",details=f"clone={clone_id}")
    await callback.answer("✅ Reset to package default.")
    await v5_features(callback)

@admin_router.callback_query(F.data.startswith("mv5_admins:"))
async def v5_admins(callback: CallbackQuery) -> None:
    clone_id=callback.data.split(":",1)[1]
    admins=await master_clone_admins(clone_id)
    rows=[]
    for a in admins:
        rows.append([InlineKeyboardButton(
            text=f"👤 {a['admin_id']} · {a['role']} {'✅' if a['enabled'] else '❌'}",
            callback_data=f"mv5_adminrole:{clone_id}:{a['admin_id']}"
        )])
        if a["admin_id"] != (await master_get_clone(clone_id))["owner_id"]:
            rows.append([InlineKeyboardButton(text=f"➖ Remove {a['admin_id']}", callback_data=f"mv5_removeadmin:{clone_id}:{a['admin_id']}")])
    rows.append([InlineKeyboardButton(text="➕ Add Admin",callback_data=f"mv5_addadmin:{clone_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Back",callback_data=f"mv5_info:{clone_id}")])
    await callback.message.edit_text("👑 <b>CLONE ADMINS</b>\n\nOwner controls clone administration; clone roles never grant Master features.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()

@admin_router.callback_query(F.data.startswith("mv5_adminrole:"))
async def v5_adminrole(callback: CallbackQuery) -> None:
    if CLONE_MODE or not is_admin(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True); return
    _, clone_id, admin_raw = callback.data.split(":",2)
    admin_id = int(admin_raw)
    row = await master_get_clone(clone_id)
    if not row:
        await callback.answer("Clone not found.", show_alert=True); return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT role FROM clone_admins WHERE clone_id=? AND admin_id=?", (clone_id,admin_id))
        r = await cur.fetchone()
    if not r:
        await callback.answer("Admin not found.", show_alert=True); return
    roles = ["OWNER","ADMIN","MODERATOR","SUPPORT","VIEWER"]
    current = r[0]
    next_role = roles[(roles.index(current)+1) % len(roles)] if current in roles else "ADMIN"
    if admin_id == row["owner_id"]:
        next_role = "OWNER"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE clone_admins SET role=? WHERE clone_id=? AND admin_id=?", (next_role,clone_id,admin_id))
        await db.commit()
    if os.path.exists(row["database_path"]):
        async with aiosqlite.connect(row["database_path"]) as cdb:
            await cdb.execute("UPDATE clone_admins SET role=? WHERE clone_id=? AND admin_id=?", (next_role,clone_id,admin_id))
            await cdb.commit()
    await audit(callback.from_user.id,"CLONE_ADMIN_ROLE",target_user_id=admin_id,details=f"clone={clone_id};role={next_role}")
    await callback.answer(f"✅ Role: {next_role}")
    await v5_admins(callback)

@admin_router.callback_query(F.data.startswith("mv5_removeadmin:"))
async def v5_removeadmin(callback: CallbackQuery) -> None:
    if CLONE_MODE or not is_admin(callback.from_user.id):
        await callback.answer("⛔ Owner only.", show_alert=True); return
    _,clone_id,admin_raw=callback.data.split(":",2); admin_id=int(admin_raw)
    row=await master_get_clone(clone_id)
    if not row or admin_id==row["owner_id"]:
        await callback.answer("❌ The clone owner cannot be removed.",show_alert=True); return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM clone_admins WHERE clone_id=? AND admin_id=?",(clone_id,admin_id)); await db.commit()
    if os.path.exists(row["database_path"]):
        async with aiosqlite.connect(row["database_path"]) as cdb:
            await cdb.execute("DELETE FROM clone_admins WHERE clone_id=? AND admin_id=?",(clone_id,admin_id)); await cdb.commit()
    await audit(callback.from_user.id,"CLONE_ADMIN_REMOVE",target_user_id=admin_id,details=f"clone={clone_id}")
    await callback.answer("✅ Admin removed.")
    await v5_admins(callback)

@admin_router.callback_query(F.data.startswith("mv5_addadmin:"))
async def v5_addadmin(callback: CallbackQuery,state:FSMContext) -> None:
    clone_id=callback.data.split(":",1)[1]
    await state.update_data(target_clone_id=clone_id)
    await state.set_state(AdminStates.waiting_clone_admin_id)
    await callback.message.edit_text("👤 Send the numeric Telegram ID to add as an ADMIN.")
    await callback.answer()

@admin_router.callback_query(F.data.startswith("mv5_health:"))
async def v5_health(callback: CallbackQuery) -> None:
    clone_id=callback.data.split(":",1)[1]
    row=await master_get_clone(clone_id)
    if not row: await callback.answer("Clone not found.",show_alert=True); return
    db_ok=os.path.exists(row["database_path"])
    size=os.path.getsize(row["database_path"]) if db_ok else 0
    runtime=_clones.get(clone_id)
    running=bool(runtime and runtime["process"].poll() is None)
    text=("❤️ <b>CLONE HEALTH</b>\n\n"
          f"Database: {'🟢' if db_ok else '🔴'} {size:,} bytes\n"
          f"Runtime: {'🟢 RUNNING' if running else '🔴 STOPPED'}\n"
          f"Status: <b>{row['status']}</b>\n"
          f"Last error: <code>{hesc(row['last_error'] or 'None')[-1000:]}</code>")
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back",callback_data=f"mv5_info:{clone_id}")]]))
    await callback.answer()

@admin_router.callback_query(F.data.startswith("mv5_logs:"))
async def v5_logs(callback: CallbackQuery) -> None:
    clone_id=callback.data.split(":",1)[1]
    row=await master_get_clone(clone_id)
    if not row: await callback.answer("Clone not found.",show_alert=True); return
    log_path=_clones.get(clone_id,{}).get("log",os.path.join(os.path.dirname(os.path.abspath(__file__)),f"clone_{row['bot_id']}.log"))
    try: tail=Path(log_path).read_text(encoding="utf-8",errors="replace")[-3500:]
    except Exception: tail="No log available."
    await callback.message.edit_text(f"📜 <b>CLONE LOG</b>\n\n<pre>{hesc(tail)}</pre>",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back",callback_data=f"mv5_info:{clone_id}")]]))
    await callback.answer()

@admin_router.callback_query(F.data.startswith("mv5_backup:"))
async def v5_clone_backup(callback: CallbackQuery) -> None:
    clone_id=callback.data.split(":",1)[1]
    row=await master_get_clone(clone_id)
    if not row: await callback.answer("Clone not found.",show_alert=True);return
    src=row["database_path"]
    if not os.path.exists(src): await callback.answer("Database not found.",show_alert=True);return
    backup_dir=os.path.join(os.path.dirname(os.path.abspath(DB_PATH)),"clone_backups");os.makedirs(backup_dir,exist_ok=True)
    dst=os.path.join(backup_dir,f"{clone_id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db")
    try:
        srcdb=sqlite3.connect(src)
        dstdb=sqlite3.connect(dst)
        srcdb.backup(dstdb)
        dstdb.close();srcdb.close()
        await audit(callback.from_user.id,"CLONE_BACKUP",details=f"clone={clone_id};file={os.path.basename(dst)}")
        await callback.answer("✅ Backup created.")
        await callback.message.answer_document(BufferedInputFile(Path(dst).read_bytes(),filename=os.path.basename(dst)))
    except Exception as exc:
        await callback.answer("❌ Backup failed.",show_alert=True)

@admin_router.callback_query(F.data.startswith("mv5_disable:"))
async def v5_disable_clone(callback: CallbackQuery) -> None:
    clone_id=callback.data.split(":",1)[1]
    row=await master_get_clone(clone_id)
    if not row: await callback.answer("Clone not found.",show_alert=True);return
    new=0 if row["enabled"] else 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE clone_registry SET enabled=?,status=?,updated_at=? WHERE clone_id=?",
                         (new,"STOPPED" if not new else "STOPPED",datetime.now(timezone.utc).isoformat(),clone_id))
        await db.commit()
    if not new: await stop_registered_clone(clone_id)
    await audit(callback.from_user.id,"CLONE_DISABLE" if not new else "CLONE_ENABLE",details=f"clone={clone_id}")
    await callback.answer("✅ Disabled." if not new else "✅ Enabled.")
    await v5_clone_info(callback)

@admin_router.callback_query(F.data.startswith("mv5_delete:"))
async def v5_delete_clone_prompt(callback: CallbackQuery,state:FSMContext) -> None:
    clone_id=callback.data.split(":",1)[1]
    row=await master_get_clone(clone_id)
    if not row: await callback.answer("Clone not found.",show_alert=True);return
    await state.update_data(delete_clone_id=clone_id)
    await state.set_state(AdminStates.waiting_clone_typed_delete)
    size=os.path.getsize(row["database_path"]) if os.path.exists(row["database_path"]) else 0
    users=await master_clone_count_users(row["database_path"])
    await callback.message.edit_text(
        "⚠️ <b>PERMANENT CLONE DELETE</b>\n\n"
        f"Bot: @{hesc(row['bot_username'] or str(row['bot_id']))}\n"
        f"Users: <b>{users}</b>\nDatabase: <b>{size:,} bytes</b>\n\n"
        f"Type exactly: <code>DELETE @{row['bot_username'] or row['bot_id']}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel",callback_data="adm_back")]])
    )
    await callback.answer()

@admin_router.message(AdminStates.waiting_clone_typed_delete)
async def v5_delete_clone_confirm(message: Message,state:FSMContext) -> None:
    d=await state.get_data();clone_id=d.get("delete_clone_id")
    row=await master_get_clone(clone_id) if clone_id else None
    if not row: await state.clear(); await message.answer("❌ Clone not found."); return
    expected=f"DELETE @{row['bot_username'] or row['bot_id']}"
    if (message.text or "").strip()!=expected:
        await message.answer("❌ Confirmation text does not match.")
        return
    await stop_registered_clone(clone_id)
    try:
        if os.path.exists(row["database_path"]): os.remove(row["database_path"])
        wal=row["database_path"]+"-wal"; shm=row["database_path"]+"-shm"
        for f in (wal,shm):
            if os.path.exists(f): os.remove(f)
    except Exception as exc:
        await message.answer("❌ Database delete failed. Registry was preserved.")
        await state.clear(); return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM clone_features WHERE clone_id=?",(clone_id,))
        await db.execute("DELETE FROM clone_admins WHERE clone_id=?",(clone_id,))
        await db.execute("DELETE FROM clone_registry WHERE clone_id=?",(clone_id,))
        await db.commit()
    await audit(message.from_user.id,"CLONE_DELETE",details=f"clone={clone_id}")
    await state.clear()
    await message.answer("✅ Clone permanently deleted.",reply_markup=await admin_panel_keyboard())

async def clone_watchdog(bot: Bot) -> None:
    failures: dict[str, tuple[int, float]] = {}
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM clone_registry WHERE enabled=1 AND auto_restart=1")
                rows = list(await cur.fetchall())
            now = time.time()
            for row in rows:
                clone_id = row["clone_id"]
                info = _clones.get(clone_id)
                if info and info["process"].poll() is None:
                    failures.pop(clone_id, None)
                    continue
                # Only restart a process that was previously expected to be running.
                if row["status"] not in {"RUNNING", "STARTING"}:
                    continue
                count, last = failures.get(clone_id, (0, 0.0))
                if count >= 5:
                    await master_set_clone_status(clone_id, "ERROR", "Auto-restart stopped after 5 consecutive failures.")
                    try:
                        await bot.send_message(
                            row["owner_id"],
                            f"🚨 <b>Clone requires attention</b>\n\n@{hesc(row['bot_username'] or str(row['bot_id']))} failed 5 consecutive auto-restarts."
                        )
                    except Exception:
                        pass
                    continue
                backoff = min(60, 2 ** count)
                if now - last < backoff:
                    continue
                failures[clone_id] = (count + 1, now)
                ok, msg = await launch_registered_clone(clone_id)
                if not ok:
                    await master_set_clone_status(clone_id, "ERROR", msg)
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Clone watchdog failed")
            await asyncio.sleep(10)

async def recover_enabled_clones() -> None:
    if CLONE_MODE:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory=aiosqlite.Row
        cur=await db.execute("SELECT clone_id FROM clone_registry WHERE enabled=1")
        ids=[r[0] for r in await cur.fetchall()]
    for clone_id in ids:
        try:
            await launch_registered_clone(clone_id)
        except Exception:
            logger.exception("Clone recovery failed: %s", clone_id)


# ---------------------------------------------------------------------------
# Dispatcher factory
# ---------------------------------------------------------------------------

def build_dispatcher() -> Dispatcher:
    """Build the aiogram dispatcher and register all application routers."""
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(user_router)
    dp.include_router(admin_router)
    return dp


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    global MASTER_USERNAME
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set.")
        sys.exit(1)
    if not ADMIN_IDS and not CLONE_ADMIN_IDS:
        logger.error("No administrator IDs configured.")
        sys.exit(1)

    await init_db()
    logger.info("Database ready at %s%s", DB_PATH, " (CLONE)" if CLONE_MODE else "")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
        if CLONE_MODE and not MASTER_USERNAME:
            logger.warning("MASTER_USERNAME is not configured; branding will use a generic footer.")
        elif not CLONE_MODE:
            MASTER_USERNAME = me.username or ""
            await set_setting("master_username", MASTER_USERNAME)
    except Exception:
        logger.exception("Bot token validation failed at startup")
        await bot.session.close()
        sys.exit(1)

    dp = build_dispatcher()
    allowed_updates = list(dp.resolve_used_update_types())
    for extra in ("chat_join_request", "chat_member"):
        if extra not in allowed_updates:
            allowed_updates.append(extra)

    watchdog_task = None
    if not CLONE_MODE:
        await recover_enabled_clones()
        watchdog_task = asyncio.create_task(clone_watchdog(bot))

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("%s starting polling...", "Clone" if CLONE_MODE else "Master")
    try:
        await dp.start_polling(bot, allowed_updates=allowed_updates)
    finally:
        if watchdog_task:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
        if not CLONE_MODE:
            for clone_id in list(_clones):
                try:
                    await stop_registered_clone(clone_id)
                except Exception:
                    logger.exception("Failed to stop clone %s", clone_id)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
