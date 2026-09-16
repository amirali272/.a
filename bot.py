#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بات قرعه‌کشی + آمار و لول + مدیریت گروه + قیمت بازار + چک آی‌پی + تبلیغ کانال
نسخه‌ی بازنویسی‌شده با رفع باگ ضد اسپم و امکانات اضافه.

نیازمندی‌ها:
    python >= 3.10
    pip install "python-telegram-bot[job-queue]" httpx openpyxl python-dotenv

تنظیمات:
    همه‌ی مقادیر قابل‌تنظیم (توکن، آیدی مالک‌ها و ...) از فایل .env خوانده می‌شوند،
    نه از داخل این فایل. یک نسخه از .env.example بساز به نام .env کنار همین فایل
    و مقادیر را پر کن. لیست کامل کلیدها و توضیح هرکدام در .env.example هست.
    اگر .env نبود یا پکیج python-dotenv نصب نباشد، بات از متغیرهای محیطی سیستم
    (export ...) می‌خواند و در لاگ هشدار می‌دهد.

نکته‌ی مهم برای کار کردن ضد اسپم:
    1) در BotFather دستور /setprivacy را روی Disable بگذار،
       سپس بات را از گروه خارج و دوباره اضافه کن.
    2) بات باید ادمین گروه باشد با دسترسی «حذف پیام» و «محدود کردن اعضا».
    3) با دستور /antispam یا «ضد اسپم» وضعیت و دسترسی‌ها را چک کن.
"""

import asyncio
import html
import json
import logging
import random
import re
import shutil
import sqlite3
import time
import os
import tempfile
import ipaddress
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # python < 3.9
    ZoneInfo = None

import httpx

try:
    from dotenv import load_dotenv
    _DOTENV_AVAILABLE = True
except ImportError:
    _DOTENV_AVAILABLE = False

from telegram import (
    Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError, BadRequest

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- بارگذاری فایل .env (اگر وجود داشته باشد) ---
# ترتیب جست‌وجو: کنار خودِ bot.py، سپس دایرکتوری اجرا (pwd).
# متغیرهایی که از قبل در محیط ست شده‌اند (export/docker env) همیشه اولویت دارند
# و با .env بازنویسی نمی‌شوند.
_ENV_PATH = os.path.join(BASE_DIR, ".env")
if _DOTENV_AVAILABLE:
    if os.path.exists(_ENV_PATH):
        load_dotenv(_ENV_PATH, override=False)
        logger.info(f"فایل .env بارگذاری شد: {_ENV_PATH}")
    elif load_dotenv(override=False):   # دنبال .env در pwd هم می‌گردد
        logger.info("فایل .env از دایرکتوری جاری بارگذاری شد.")
    else:
        logger.warning(
            f"فایل .env پیدا نشد ({_ENV_PATH}). از .env.example یک کپی به نام "
            f".env بساز و مقادیر را پر کن، یا متغیرها را با export ست کن."
        )
else:
    logger.warning(
        "پکیج python-dotenv نصب نیست — فایل .env خوانده نمی‌شود. "
        "نصب: pip install python-dotenv"
    )

DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "lottery.db"))

# توکن فقط از متغیر محیطی خوانده می‌شود.
# هرگز توکن را داخل کد نگذار — اگر قبلاً گذاشته بودی، از BotFather با /revoke باطلش کن.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

PROXY_URL = os.environ.get("PROXY_URL", "").strip()

DEFAULT_MIN_NUM = 1
DEFAULT_MAX_NUM = 1000
RANGE_HARD_CAP = 1_000_000
RANGE_PRESETS = [(1, 100), (1, 500), (1, 1000), (1, 2000), (1, 5000)]

OWNER_IDS = {
    int(x.strip())
    for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

GROUP_TYPES = ("group", "supergroup")

# مقدار XP هر پیام و فاصله‌ی حداقلی بین دو XP (ثانیه)
XP_MIN = int(os.environ.get("XP_MIN", "1"))
XP_MAX = int(os.environ.get("XP_MAX", "3"))
XP_COOLDOWN = int(os.environ.get("XP_COOLDOWN", "0"))  # 0 = بدون محدودیت

# مدت سایلنت خودکار هنگام فلاد
FLOOD_MUTE_MINUTES = max(1, int(os.environ.get("FLOOD_MUTE_MINUTES", "5")))

# منطقه‌ی زمانی برای «حالت شب»
TZ_NAME = os.environ.get("BOT_TZ", "Asia/Tehran").strip()
if ZoneInfo:
    try:
        LOCAL_TZ = ZoneInfo(TZ_NAME)
    except Exception:
        logger.warning(f"منطقه‌ی زمانی «{TZ_NAME}» نامعتبر است — از زمان سیستم استفاده می‌شود.")
        LOCAL_TZ = None
else:
    LOCAL_TZ = None


# ---------------------------------------------------------------------------
# ابزارهای زمان (همه‌چیز با timezone ذخیره می‌شود)
# ---------------------------------------------------------------------------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _localnow() -> datetime:
    return datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now().astimezone()


def _parse_dt(value):
    """رشته‌ی ذخیره‌شده را به datetime آگاه از timezone تبدیل می‌کند.
    رکوردهای قدیمیِ بدون timezone را زمان محلی در نظر می‌گیرد."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ) if LOCAL_TZ else dt.astimezone()
    return dt


# ---------------------------------------------------------------------------
# ایموجی‌ها
# ---------------------------------------------------------------------------
EMOJI_CONFIG_PATH = os.path.join(BASE_DIR, "emojis.json")

# ایموجی سفارشی (<tg-emoji>) فقط برای بات‌های پرمیوم کار می‌کند؛
# اگر روشن باشد و بات پرمیوم نباشد، تلگرام کل پیام را رد می‌کند.
DISABLE_CUSTOM_EMOJI = os.environ.get("DISABLE_CUSTOM_EMOJI", "1") == "1"

EMOJI_DEFS = {
    # === قرعه‌کشی ===
    "party":       {"fallback": "🎉", "custom_emoji_id": None, "usage": "پیام شروع قرعه‌کشی"},
    "lock":        {"fallback": "🔒", "custom_emoji_id": None, "usage": "بسته شدن ثبت‌نام"},
    "trophy":      {"fallback": "🏆", "custom_emoji_id": None, "usage": "عنوان نتیجه‌ی قرعه‌کشی"},
    "check":       {"fallback": "✅", "custom_emoji_id": None, "usage": "تایید موفق"},
    "cross":       {"fallback": "❌", "custom_emoji_id": None, "usage": "خطا / نبود دسترسی"},
    "warn":        {"fallback": "⚠️", "custom_emoji_id": None, "usage": "هشدار"},
    "medal1":      {"fallback": "🥇", "custom_emoji_id": None, "usage": "رتبه‌ی اول"},
    "medal2":      {"fallback": "🥈", "custom_emoji_id": None, "usage": "رتبه‌ی دوم"},
    "medal3":      {"fallback": "🥉", "custom_emoji_id": None, "usage": "رتبه‌ی سوم"},
    "doc":         {"fallback": "📄", "custom_emoji_id": None, "usage": "فایل اکسل خروجی"},
    "scroll":      {"fallback": "📜", "custom_emoji_id": None, "usage": "تاریخچه"},
    "info":        {"fallback": "ℹ️", "custom_emoji_id": None, "usage": "پیام اطلاعاتی"},
    "id":          {"fallback": "🆔", "custom_emoji_id": None, "usage": "آیدی عددی کاربر"},
    "gift":        {"fallback": "🎁", "custom_emoji_id": None, "usage": "تبریک به برنده"},
    "dice":        {"fallback": "🎲", "custom_emoji_id": None, "usage": "وضعیت باز بودن"},
    "chart":       {"fallback": "📊", "custom_emoji_id": None, "usage": "وضعیت و آمار"},
    "hourglass":   {"fallback": "⏳", "custom_emoji_id": None, "usage": "نیاز به بستن ثبت‌نام"},
    "pin":         {"fallback": "📌", "custom_emoji_id": None, "usage": "بولت لیست‌ها"},
    "sparkle":     {"fallback": "✨", "custom_emoji_id": None, "usage": "خوش‌آمدگویی و راهنما"},
    "love":        {"fallback": "😍", "custom_emoji_id": None, "usage": "پایان پیام تبریک"},
    "menu":        {"fallback": "🧭", "custom_emoji_id": None, "usage": "پنل مدیریت"},
    "target":      {"fallback": "🎯", "custom_emoji_id": None, "usage": "هر گروه در لیست"},
    "back":        {"fallback": "◀️", "custom_emoji_id": None, "usage": "بازگشت"},
    "refresh":     {"fallback": "🔄", "custom_emoji_id": None, "usage": "تازه‌سازی"},
    "range":       {"fallback": "🎚️", "custom_emoji_id": None, "usage": "تنظیم بازه‌ی عدد"},
    "leaderboard": {"fallback": "🏅", "custom_emoji_id": None, "usage": "جدول برترین‌ها"},
    "network":     {"fallback": "📬", "custom_emoji_id": None, "usage": "نتیجه‌ی چک آی‌پی"},

    # === آمار و لول ===
    "level":       {"fallback": "📈", "custom_emoji_id": None, "usage": "لول و XP"},
    "star":        {"fallback": "⭐", "custom_emoji_id": None, "usage": "XP و امتیاز"},
    "fire":        {"fallback": "🔥", "custom_emoji_id": None, "usage": "استریک و فعالیت"},
    "crown":       {"fallback": "👑", "custom_emoji_id": None, "usage": "رتبه‌ی اول"},
    "up":          {"fallback": "🚀", "custom_emoji_id": None, "usage": "لول‌آپ"},
    "bar_full":    {"fallback": "▰", "custom_emoji_id": None, "usage": "نوار پیشرفت پر"},
    "bar_empty":   {"fallback": "▱", "custom_emoji_id": None, "usage": "نوار پیشرفت خالی"},

    # === مدیریت گروه ===
    "shield":      {"fallback": "🛡️", "custom_emoji_id": None, "usage": "ضد اسپم"},
    "ban":         {"fallback": "🚫", "custom_emoji_id": None, "usage": "بن"},
    "mute":        {"fallback": "🔇", "custom_emoji_id": None, "usage": "سایلنت"},
    "kick":        {"fallback": "👢", "custom_emoji_id": None, "usage": "اخراج"},
    "hello":       {"fallback": "👋", "custom_emoji_id": None, "usage": "خوش‌آمد عضو جدید"},
    "rule":        {"fallback": "📋", "custom_emoji_id": None, "usage": "قوانین گروه"},
    "warning":     {"fallback": "⚠️", "custom_emoji_id": None, "usage": "اخطار"},
    "link":        {"fallback": "🔗", "custom_emoji_id": None, "usage": "ضد لینک"},
    "night":       {"fallback": "🌙", "custom_emoji_id": None, "usage": "حالت شب"},
    "clock":       {"fallback": "⏰", "custom_emoji_id": None, "usage": "زمان"},
    "trash":       {"fallback": "🗑️", "custom_emoji_id": None, "usage": "حذف پیام"},
    "gear":        {"fallback": "⚙️", "custom_emoji_id": None, "usage": "تنظیمات"},

    # === گزارش اسکم / تبر ===
    "scam":        {"fallback": "🪓", "custom_emoji_id": None, "usage": "تبرزن / اسکمر"},
    "report":      {"fallback": "📮", "custom_emoji_id": None, "usage": "ثبت گزارش اسکم"},
    "evidence":    {"fallback": "🖼️", "custom_emoji_id": None, "usage": "مستندات گزارش"},
    "card":        {"fallback": "💳", "custom_emoji_id": None, "usage": "شماره کارت/حساب"},
    "judge":       {"fallback": "⚖️", "custom_emoji_id": None, "usage": "بررسی گزارش"},
    "approve":     {"fallback": "✅", "custom_emoji_id": None, "usage": "تایید گزارش"},
    "reject":      {"fallback": "🚷", "custom_emoji_id": None, "usage": "رد گزارش"},
    "pending":     {"fallback": "🕐", "custom_emoji_id": None, "usage": "در انتظار بررسی"},
    "blacklist":   {"fallback": "📕", "custom_emoji_id": None, "usage": "لیست سیاه اسکمرها"},
    "siren":       {"fallback": "🚨", "custom_emoji_id": None, "usage": "هشدار اسکمر در گروه"},
    "on":          {"fallback": "🟢", "custom_emoji_id": None, "usage": "فعال"},
    "off":         {"fallback": "🔴", "custom_emoji_id": None, "usage": "غیرفعال"},
    "pin2":        {"fallback": "📍", "custom_emoji_id": None, "usage": "پین کردن پیام"},
    "robot":       {"fallback": "🤖", "custom_emoji_id": None, "usage": "وضعیت خود بات"},

    # === کپچا و لاگ اقدامات ===
    "captcha":     {"fallback": "🧩", "custom_emoji_id": None, "usage": "کپچا اعضای جدید"},
    "timer":       {"fallback": "⏱️", "custom_emoji_id": None, "usage": "مهلت کپچا"},
    "audit":       {"fallback": "🗂️", "custom_emoji_id": None, "usage": "لاگ اقدامات ادمین‌ها"},
    "system":      {"fallback": "⚙️", "custom_emoji_id": None, "usage": "اقدام خودکار بات"},

    # === قیمت بازار: دلار / ارز / طلا ===
    "usd":         {"fallback": "💵", "custom_emoji_id": None, "usage": "قیمت دلار"},
    "currency":    {"fallback": "💱", "custom_emoji_id": None, "usage": "قیمت هر ارز خارجی"},
    "gold_oz":     {"fallback": "🪙", "custom_emoji_id": None, "usage": "انس طلای جهانی"},
    "gold24":      {"fallback": "🥇", "custom_emoji_id": None, "usage": "طلای ۲۴ عیار"},
    "gold18":      {"fallback": "🥇", "custom_emoji_id": None, "usage": "طلای ۱۸ عیار"},
    "globe":       {"fallback": "🌐", "custom_emoji_id": None, "usage": "IP در /check"},
    "location":    {"fallback": "📍", "custom_emoji_id": None, "usage": "منطقه در /check"},
    "city":        {"fallback": "🏙️", "custom_emoji_id": None, "usage": "شهر در /check"},
    "office":      {"fallback": "🏢", "custom_emoji_id": None, "usage": "ASN در /check"},
}

COUNTRY_FLAG_DEFS = {
    "ch": "🇨🇭", "de": "🇩🇪", "es": "🇪🇸", "fi": "🇫🇮", "fr": "🇫🇷",
    "gb": "🇬🇧", "ir": "🇮🇷", "it": "🇮🇹", "nl": "🇳🇱", "pl": "🇵🇱",
    "se": "🇸🇪", "tr": "🇹🇷", "us": "🇺🇸",
}
for _code, _flag in COUNTRY_FLAG_DEFS.items():
    EMOJI_DEFS[f"flag_{_code}"] = {
        "fallback": _flag, "custom_emoji_id": None,
        "usage": f"پرچم {_code.upper()} در /check",
    }

_emoji_cache = {"mtime": None, "data": {}}


def _load_custom_emoji_ids() -> dict:
    """-> {key: {"id": "...", "alt": "🎉" یا None}}"""
    global _emoji_cache
    try:
        mtime = os.path.getmtime(EMOJI_CONFIG_PATH)
    except OSError:
        return {}
    if _emoji_cache["mtime"] != mtime:
        data = {}
        try:
            with open(EMOJI_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for key, value in raw.items():
                if not value:
                    continue
                if isinstance(value, dict):
                    eid = value.get("id")
                    alt = value.get("alt") or None
                else:
                    eid, alt = str(value), None
                if eid:
                    data[key] = {"id": str(eid), "alt": alt}
        except Exception as e:
            logger.warning(f"خطا در خواندن emojis.json: {e}")
        _emoji_cache = {"mtime": mtime, "data": data}
    return _emoji_cache["data"]


def _ensure_emoji_config_exists():
    existing = {}
    if os.path.exists(EMOJI_CONFIG_PATH):
        try:
            with open(EMOJI_CONFIG_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    changed = False
    for key, info in EMOJI_DEFS.items():
        if key not in existing:
            eid = info.get("custom_emoji_id")
            existing[key] = {"id": eid, "alt": info.get("fallback")} if eid else None
            changed = True
    if changed or not os.path.exists(EMOJI_CONFIG_PATH):
        try:
            with open(EMOJI_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            logger.info(f"emojis.json به‌روزرسانی شد: {EMOJI_CONFIG_PATH}")
        except OSError as e:
            logger.warning(f"نوشتن emojis.json ناموفق: {e}")


def E(key: str) -> str:
    fallback = EMOJI_DEFS.get(key, {}).get("fallback", "")
    if DISABLE_CUSTOM_EMOJI:
        return fallback
    entry = _load_custom_emoji_ids().get(key)
    custom_id = entry["id"] if entry else EMOJI_DEFS.get(key, {}).get("custom_emoji_id")
    if not custom_id:
        return fallback
    alt = (entry.get("alt") if entry else None) or fallback
    return f'<tg-emoji emoji-id="{custom_id}">{alt}</tg-emoji>'


def flag_emoji(country_code: str) -> str:
    code = (country_code or "").lower()
    if f"flag_{code}" in EMOJI_DEFS:
        return E(f"flag_{code}")
    cc = code.upper()
    if len(cc) == 2 and cc.isalpha():
        return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in cc)
    return "🏳️"


def btn(key: str, label: str, **button_kwargs) -> InlineKeyboardButton:
    """دکمه با ایموجی."""
    fallback = EMOJI_DEFS.get(key, {}).get("fallback", "")
    icon_id = None
    if not DISABLE_CUSTOM_EMOJI:
        entry = _load_custom_emoji_ids().get(key)
        icon_id = (entry or {}).get("id") or EMOJI_DEFS.get(key, {}).get("custom_emoji_id")
    if icon_id:
        try:
            return InlineKeyboardButton(text=label, icon_custom_emoji_id=icon_id,
                                        **button_kwargs)
        except TypeError:
            logger.warning(
                "icon_custom_emoji_id پشتیبانی نمی‌شود — "
                "python-telegram-bot را به‌روز کن: pip install -U python-telegram-bot"
            )
    text = f"{fallback} {label}" if fallback else label
    return InlineKeyboardButton(text=text, **button_kwargs)


DIVIDER = "┈┈┈┈┈┈┈┈┈┈┈┈┈"
_TAG_RE = re.compile(r"<[^>]+>")


def esc(text) -> str:
    return html.escape(str(text)) if text is not None else ""


def strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or ""))


async def reply(update: Update, text: str, **kwargs):
    """ارسال امن: اگر تلگرام HTML را رد کرد، نسخه‌ی ساده می‌فرستد."""
    msg = update.effective_message
    if msg is None:
        return None
    try:
        return await msg.reply_text(text, parse_mode="HTML", **kwargs)
    except BadRequest as e:
        logger.warning(f"ارسال HTML ناموفق ({e}) — بدون فرمت می‌فرستم")
        try:
            return await msg.reply_text(strip_html(text), **kwargs)
        except TelegramError as e2:
            logger.warning(f"ارسال پیام کلاً ناموفق: {e2}")
    except TelegramError as e:
        logger.warning(f"ارسال پیام ناموفق: {e}")
    return None


async def safe_edit(query, text: str, **kwargs):
    try:
        return await query.edit_message_text(text, parse_mode="HTML", **kwargs)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return None
        logger.warning(f"ویرایش HTML ناموفق ({e}) — بدون فرمت")
        try:
            return await query.edit_message_text(strip_html(text), **kwargs)
        except TelegramError as e2:
            logger.warning(f"ویرایش پیام ناموفق: {e2}")
    except TelegramError as e:
        logger.warning(f"ویرایش پیام ناموفق: {e}")
    return None


async def announce(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs):
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text,
                                              parse_mode="HTML", **kwargs)
    except BadRequest as e:
        logger.warning(f"ارسال HTML به {chat_id} ناموفق ({e}) — بدون فرمت")
        try:
            return await context.bot.send_message(chat_id=chat_id,
                                                  text=strip_html(text), **kwargs)
        except TelegramError as e2:
            logger.warning(f"ارسال به {chat_id} ناموفق: {e2}")
    except TelegramError as e:
        logger.warning(f"ارسال به {chat_id} ناموفق: {e}")
    return None


# ---------------------------------------------------------------------------
# دیتابیس
# ---------------------------------------------------------------------------
@contextmanager
def db(commit: bool = False):
    """کانکشن امن با timeout؛ همیشه بسته می‌شود."""
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    try:
        yield conn
        if commit:
            conn.commit()
    finally:
        conn.close()


def _ensure_columns(c, table: str, columns: dict):
    """ستون‌های گم‌شده را روی دیتابیس‌های قدیمی اضافه می‌کند."""
    c.execute(f"PRAGMA table_info({table})")
    existing = {r[1] for r in c.fetchall()}
    if not existing:
        return
    for col, ddl in columns.items():
        if col not in existing:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                logger.info(f"ستون «{col}» به جدول «{table}» اضافه شد.")
            except sqlite3.OperationalError as e:
                logger.warning(f"افزودن ستون {col} به {table} ناموفق: {e}")


def init_db():
    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")

        c.execute("""
            CREATE TABLE IF NOT EXISTS lotteries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                finished_at TIMESTAMP,
                min_num INTEGER,
                max_num INTEGER
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lottery_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                number INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(lottery_id, number),
                UNIQUE(lottery_id, user_id)
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS winners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lottery_id INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                number INTEGER NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS known_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                type TEXT,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY,
                min_num INTEGER NOT NULL,
                max_num INTEGER NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_stats (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                message_count INTEGER DEFAULT 0,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                last_xp_at TIMESTAMP,
                streak_days INTEGER DEFAULT 0,
                last_streak_date TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS group_management (
                chat_id INTEGER PRIMARY KEY,
                antispam_enabled INTEGER DEFAULT 1,
                antilink_enabled INTEGER DEFAULT 0,
                welcome_enabled INTEGER DEFAULT 1,
                welcome_text TEXT,
                rules_text TEXT,
                night_mode_enabled INTEGER DEFAULT 0,
                night_start_hour INTEGER DEFAULT 0,
                night_end_hour INTEGER DEFAULT 7,
                max_warnings INTEGER DEFAULT 3,
                flood_limit INTEGER DEFAULT 5,
                flood_window INTEGER DEFAULT 10,
                antiforward_enabled INTEGER DEFAULT 0,
                flood_action TEXT DEFAULT 'mute',
                captcha_enabled INTEGER DEFAULT 0,
                captcha_minutes INTEGER DEFAULT 3,
                captcha_action TEXT DEFAULT 'kick'
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS captcha_pending (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                correct_emoji TEXT NOT NULL,
                message_id INTEGER,
                attempts INTEGER DEFAULT 0,
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                actor_id INTEGER,
                actor_name TEXT,
                action TEXT NOT NULL,
                target_id INTEGER,
                target_name TEXT,
                detail TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_warnings (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER DEFAULT 0,
                last_reason TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS muted_users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                until TIMESTAMP,
                reason TEXT,
                PRIMARY KEY (chat_id, user_id)
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS message_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                sent_at REAL NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS scam_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL,
                reporter_name TEXT,
                reporter_username TEXT,
                accused_id INTEGER,
                accused_raw TEXT,
                amount TEXT,
                description TEXT,
                evidence TEXT,
                status TEXT DEFAULT 'pending',
                source_chat_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_by INTEGER,
                reviewed_at TIMESTAMP,
                review_note TEXT
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS scammers (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                report_id INTEGER,
                note TEXT,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS whitelist (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )""")

        # --- مهاجرت دیتابیس‌های قدیمی (قبل از ساخت ایندکس‌ها) ---
        _ensure_columns(c, "lotteries", {"min_num": "INTEGER", "max_num": "INTEGER"})
        _ensure_columns(c, "user_stats", {
            "username": "TEXT", "full_name": "TEXT",
            "message_count": "INTEGER DEFAULT 0", "xp": "INTEGER DEFAULT 0",
            "level": "INTEGER DEFAULT 1", "last_xp_at": "TIMESTAMP",
            "streak_days": "INTEGER DEFAULT 0", "last_streak_date": "TEXT",
            "joined_at": "TIMESTAMP",
        })
        _ensure_columns(c, "group_management", {
            "antispam_enabled": "INTEGER DEFAULT 1",
            "antilink_enabled": "INTEGER DEFAULT 0",
            "welcome_enabled": "INTEGER DEFAULT 1",
            "welcome_text": "TEXT", "rules_text": "TEXT",
            "night_mode_enabled": "INTEGER DEFAULT 0",
            "night_start_hour": "INTEGER DEFAULT 0",
            "night_end_hour": "INTEGER DEFAULT 7",
            "max_warnings": "INTEGER DEFAULT 3",
            "flood_limit": "INTEGER DEFAULT 5",
            "flood_window": "INTEGER DEFAULT 10",
            "antiforward_enabled": "INTEGER DEFAULT 0",
            "flood_action": "TEXT DEFAULT 'mute'",
            "captcha_enabled": "INTEGER DEFAULT 0",
            "captcha_minutes": "INTEGER DEFAULT 3",
            "captcha_action": "TEXT DEFAULT 'kick'",
        })

        # --- ایندکس‌ها ---
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_msg_log "
            "ON message_log(chat_id, user_id, sent_at)",
            "CREATE INDEX IF NOT EXISTS idx_entries_lottery ON entries(lottery_id)",
            "CREATE INDEX IF NOT EXISTS idx_lotteries_chat ON lotteries(chat_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_stats_chat_xp ON user_stats(chat_id, xp)",
            "CREATE INDEX IF NOT EXISTS idx_winners_lottery ON winners(lottery_id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_chat ON audit_log(chat_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(chat_id, target_id)",
            "CREATE INDEX IF NOT EXISTS idx_captcha_expires ON captcha_pending(expires_at)",
        ):
            try:
                c.execute(idx_sql)
            except sqlite3.OperationalError as e:
                logger.warning(f"ساخت ایندکس ناموفق: {e}")
    logger.info(f"دیتابیس آماده است: {DB_PATH}")


# ---------------------------------------------------------------------------
# آمار و لول
# ---------------------------------------------------------------------------
def xp_for_level(level: int) -> int:
    """XP تجمعی لازم برای رسیدن به این لول. لول ۱ همیشه صفر است."""
    if level <= 1:
        return 0
    return int(50 * ((level - 1) ** 1.5))


def calculate_level(xp: int) -> int:
    xp = max(0, int(xp or 0))
    level = 1
    while level < 200 and xp >= xp_for_level(level + 1):
        level += 1
    return level


def progress_bar(current: int, total: int, length: int = 10) -> str:
    total = max(1, int(total or 0))
    current = max(0, min(int(current or 0), total))
    filled = min(length, int(length * current / total))
    return E("bar_full") * filled + E("bar_empty") * (length - filled)


def level_progress(xp: int, level: int):
    """(xp لول فعلی، xp لول بعدی، پیشرفت، لازم، نوار)"""
    xp = max(0, int(xp or 0))
    cur = xp_for_level(level)
    nxt = xp_for_level(level + 1)
    needed = max(1, nxt - cur)
    have = max(0, min(xp - cur, needed))
    return cur, nxt, have, needed, progress_bar(have, needed)


def get_user_stats(chat_id: int, user_id: int):
    """(message_count, xp, level, streak_days)"""
    with db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT message_count, xp, level, streak_days "
            "FROM user_stats WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
        row = c.fetchone()
    if not row:
        return 0, 0, 1, 0
    return (row[0] or 0), (row[1] or 0), (row[2] or 1), (row[3] or 0)


def upsert_user_stats(chat_id: int, user_id: int, username, full_name,
                      msg_delta: int = 0, xp_delta: int = 0):
    """آمار، XP، لول و استریک را به‌روزرسانی می‌کند. -> (leveled_up, level, xp)"""
    today = _localnow().date()
    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT message_count, xp, level, streak_days, last_streak_date "
            "FROM user_stats WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
        row = c.fetchone()

        if row is None:
            new_msg = max(0, msg_delta)
            new_xp = max(0, xp_delta)
            old_level = 1
            old_streak = 0
            last_streak_date = None
        else:
            new_msg = max(0, (row[0] or 0) + msg_delta)
            new_xp = max(0, (row[1] or 0) + xp_delta)
            old_level = row[2] or 1
            old_streak = row[3] or 0
            last_streak_date = row[4]

        new_streak = old_streak
        if msg_delta > 0:
            last_date = None
            if last_streak_date:
                try:
                    last_date = datetime.fromisoformat(str(last_streak_date)).date()
                except (ValueError, TypeError):
                    last_date = None
            if last_date == today:
                new_streak = max(1, old_streak)
            elif last_date == today - timedelta(days=1):
                new_streak = max(1, old_streak + 1)
            else:
                new_streak = 1
            last_streak_date = today.isoformat()

        new_level = calculate_level(new_xp)
        leveled_up = new_level > old_level

        c.execute(
            """
            INSERT INTO user_stats (
                chat_id, user_id, username, full_name,
                message_count, xp, level, last_xp_at,
                streak_days, last_streak_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                message_count=excluded.message_count,
                xp=excluded.xp,
                level=excluded.level,
                last_xp_at=CURRENT_TIMESTAMP,
                streak_days=excluded.streak_days,
                last_streak_date=excluded.last_streak_date
            """,
            (chat_id, user_id, username, full_name,
             new_msg, new_xp, new_level, new_streak, last_streak_date),
        )
    return leveled_up, new_level, new_xp


def get_top_users(chat_id: int, limit: int = 10, order_by: str = "xp"):
    col = "message_count" if order_by == "message_count" else "xp"
    with db() as conn:
        c = conn.cursor()
        c.execute(
            f"SELECT user_id, username, full_name, message_count, xp, level "
            f"FROM user_stats WHERE chat_id=? ORDER BY {col} DESC, user_id ASC LIMIT ?",
            (chat_id, limit),
        )
        return c.fetchall()


def get_user_rank(chat_id: int, user_id: int, order_by: str = "xp"):
    col = "message_count" if order_by == "message_count" else "xp"
    with db() as conn:
        c = conn.cursor()
        c.execute(f"SELECT {col} FROM user_stats WHERE chat_id=? AND user_id=?",
                  (chat_id, user_id))
        row = c.fetchone()
        if not row:
            return None
        c.execute(f"SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND {col} > ?",
                  (chat_id, row[0] or 0))
        higher = c.fetchone()[0]
    return higher + 1


def get_chat_global_stats(chat_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*), COALESCE(SUM(message_count),0), COALESCE(SUM(xp),0), "
            "COALESCE(MAX(level),1) FROM user_stats WHERE chat_id=?",
            (chat_id,),
        )
        return c.fetchone() or (0, 0, 0, 1)


def render_profile_text(chat_id: int, user, title: str = "پروفایل") -> str:
    """متن پروفایل/آمار — مشترک بین دستور و دکمه‌ی پنل."""
    msg_count, xp, level, streak = get_user_stats(chat_id, user.id)
    if not msg_count and not xp:
        return (
            f"{E('info')} هنوز آماری برای شما در این گروه ثبت نشده.\n"
            f"با چت کردن، XP و لول می‌گیری!"
        )
    _, nxt, have, needed, bar = level_progress(xp, level)
    rank = get_user_rank(chat_id, user.id) or "-"
    warns = get_warnings(chat_id, user.id)
    name = esc(getattr(user, "full_name", None) or getattr(user, "username", None) or user.id)
    uname = f"@{user.username}" if getattr(user, "username", None) else "بدون یوزرنیم"
    return (
        f"{E('id')} <b>{esc(title)}</b>\n"
        f"{DIVIDER}\n"
        f"{E('id')} نام: <b>{name}</b>\n"
        f"{E('id')} یوزرنیم: {esc(uname)}\n"
        f"{E('id')} آیدی: <code>{user.id}</code>\n"
        f"{E('level')} لول: <code>{level}</code>\n"
        f"{E('star')} XP: <code>{xp}</code> / <code>{nxt}</code>\n"
        f"{bar}  <code>{have}/{needed}</code>\n"
        f"{E('chart')} پیام‌ها: <code>{msg_count}</code>\n"
        f"{E('crown')} رتبه: <code>{rank}</code>\n"
        f"{E('fire')} استریک: <code>{streak}</code> روز\n"
        f"{E('warning')} اخطارها: <code>{warns}</code>"
    )


# ---------------------------------------------------------------------------
# مدیریت گروه — لایه دیتابیس
# ---------------------------------------------------------------------------
DEFAULT_MGMT = {
    "antispam": True, "antilink": False, "welcome": True,
    "welcome_text": None, "rules_text": None, "night_mode": False,
    "night_start": 0, "night_end": 7, "max_warnings": 3,
    "flood_limit": 5, "flood_window": 10,
    "antiforward": False, "flood_action": "mute",
    "captcha": False, "captcha_minutes": 3, "captcha_action": "kick",
}

_mgmt_cache: dict = {}          # chat_id -> (timestamp, dict)
MGMT_CACHE_TTL = 30


def get_group_mgmt(chat_id: int) -> dict:
    """با کش کوتاه، تا ضد اسپم روی هر پیام به دیتابیس نزند."""
    cached = _mgmt_cache.get(chat_id)
    if cached and time.time() - cached[0] < MGMT_CACHE_TTL:
        return dict(cached[1])
    with db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT antispam_enabled, antilink_enabled, welcome_enabled, welcome_text, "
            "rules_text, night_mode_enabled, night_start_hour, night_end_hour, "
            "max_warnings, flood_limit, flood_window, antiforward_enabled, flood_action, "
            "captcha_enabled, captcha_minutes, captcha_action "
            "FROM group_management WHERE chat_id=?",
            (chat_id,),
        )
        row = c.fetchone()
    if not row:
        data = dict(DEFAULT_MGMT)
    else:
        data = {
            "antispam": bool(row[0]), "antilink": bool(row[1]),
            "welcome": bool(row[2]), "welcome_text": row[3],
            "rules_text": row[4], "night_mode": bool(row[5]),
            "night_start": row[6] if row[6] is not None else 0,
            "night_end": row[7] if row[7] is not None else 7,
            "max_warnings": row[8] or 3,
            "flood_limit": row[9] or 5,
            "flood_window": row[10] or 10,
            "antiforward": bool(row[11]) if len(row) > 11 else False,
            "flood_action": (row[12] if len(row) > 12 and row[12] else "mute"),
            "captcha": bool(row[13]) if len(row) > 13 else False,
            "captcha_minutes": (row[14] if len(row) > 14 and row[14] else 3),
            "captcha_action": (row[15] if len(row) > 15 and row[15] else "kick"),
        }
    _mgmt_cache[chat_id] = (time.time(), dict(data))
    return data


def update_group_mgmt(chat_id: int, **kwargs):
    current = get_group_mgmt(chat_id)
    current.update(kwargs)
    with db(commit=True) as conn:
        conn.execute(
            """
            INSERT INTO group_management
            (chat_id, antispam_enabled, antilink_enabled, welcome_enabled, welcome_text,
             rules_text, night_mode_enabled, night_start_hour, night_end_hour,
             max_warnings, flood_limit, flood_window, antiforward_enabled, flood_action,
             captcha_enabled, captcha_minutes, captcha_action)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                antispam_enabled=excluded.antispam_enabled,
                antilink_enabled=excluded.antilink_enabled,
                welcome_enabled=excluded.welcome_enabled,
                welcome_text=excluded.welcome_text,
                rules_text=excluded.rules_text,
                night_mode_enabled=excluded.night_mode_enabled,
                night_start_hour=excluded.night_start_hour,
                night_end_hour=excluded.night_end_hour,
                max_warnings=excluded.max_warnings,
                flood_limit=excluded.flood_limit,
                flood_window=excluded.flood_window,
                antiforward_enabled=excluded.antiforward_enabled,
                flood_action=excluded.flood_action,
                captcha_enabled=excluded.captcha_enabled,
                captcha_minutes=excluded.captcha_minutes,
                captcha_action=excluded.captcha_action
            """,
            (
                chat_id,
                int(current["antispam"]), int(current["antilink"]), int(current["welcome"]),
                current["welcome_text"], current["rules_text"], int(current["night_mode"]),
                current["night_start"], current["night_end"], current["max_warnings"],
                current["flood_limit"], current["flood_window"],
                int(current.get("antiforward", False)),
                current.get("flood_action", "mute"),
                int(current.get("captcha", False)),
                current.get("captcha_minutes", 3),
                current.get("captcha_action", "kick"),
            ),
        )
    _mgmt_cache.pop(chat_id, None)     # کش باطل شود تا تغییر فوراً اعمال شود


# ---- لیست سفید (معاف از ضد اسپم/لینک) ----
_whitelist_cache: dict = {}
WHITELIST_CACHE_TTL = 60


def get_whitelist(chat_id: int) -> set:
    cached = _whitelist_cache.get(chat_id)
    if cached and time.time() - cached[0] < WHITELIST_CACHE_TTL:
        return cached[1]
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM whitelist WHERE chat_id=?", (chat_id,))
        ids = {r[0] for r in c.fetchall()}
    _whitelist_cache[chat_id] = (time.time(), ids)
    return ids


def add_whitelist(chat_id: int, user_id: int, added_by: int):
    with db(commit=True) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO whitelist (chat_id, user_id, added_by) VALUES (?, ?, ?)",
            (chat_id, user_id, added_by),
        )
    _whitelist_cache.pop(chat_id, None)


def remove_whitelist(chat_id: int, user_id: int) -> bool:
    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM whitelist WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        removed = c.rowcount > 0
    _whitelist_cache.pop(chat_id, None)
    return removed


# ---------------------------------------------------------------------------
# لاگ کامل اقدامات ادمین‌ها (Audit Log)
# ---------------------------------------------------------------------------
# actor_id صفر یعنی اقدام خودکارِ خودِ بات است (نه یک ادمین انسانی)
SYSTEM_ACTOR_ID = 0

AUDIT_ACTION_LABELS = {
    "warn": ("warning", "اخطار"),
    "unwarn": ("check", "کم‌کردن اخطار"),
    "resetwarns": ("check", "پاک‌کردن اخطارها"),
    "mute": ("mute", "سایلنت"),
    "unmute": ("check", "آن‌میوت"),
    "kick": ("kick", "اخراج"),
    "ban": ("ban", "بن"),
    "unban": ("check", "آن‌بن"),
    "delete_message": ("trash", "حذف پیام"),
    "purge": ("trash", "حذف گروهی"),
    "pin": ("pin2", "پین پیام"),
    "unpin": ("pin2", "برداشتن پین"),
    "whitelist_add": ("shield", "افزودن به لیست سفید"),
    "whitelist_remove": ("shield", "حذف از لیست سفید"),
    "toggle_setting": ("gear", "تغییر تنظیمات"),
    "add_admin": ("id", "افزودن ادمین"),
    "remove_admin": ("id", "حذف ادمین"),
    "scam_flag": ("scam", "علامت‌گذاری اسکمر"),
    "scam_free": ("check", "لغو علامت اسکم"),
    "scam_approve": ("approve", "تایید گزارش اسکم"),
    "scam_reject": ("reject", "رد گزارش اسکم"),
    "captcha_fail": ("captcha", "شکست کپچا (خودکار)"),
    "auto_flood_mute": ("mute", "سایلنت خودکار (فلاد)"),
    "auto_link_delete": ("link", "حذف خودکار لینک"),
    "auto_forward_delete": ("trash", "حذف خودکار فوروارد"),
    "auto_night_delete": ("night", "حذف خودکار (حالت شب)"),
}


def log_action(chat_id: int, actor_id, actor_name, action: str,
               target_id=None, target_name=None, detail: str = ""):
    """یک ردیف در لاگ اقدامات ادمین‌ها ثبت می‌کند. هرگز نباید کل درخواست را
    fail کند، پس خطاها فقط لاگ می‌شوند."""
    try:
        with db(commit=True) as conn:
            conn.execute(
                """
                INSERT INTO audit_log
                (chat_id, actor_id, actor_name, action, target_id, target_name, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, actor_id, actor_name, action, target_id, target_name, detail),
            )
    except Exception as e:
        logger.warning(f"ثبت لاگ اقدام ناموفق: {e}")


GLOBAL_CHAT_ID = 0     # برای اقدامات سراسری مثل addadmin/removeadmin که به یک گروه خاص مربوط نیستند


def get_audit_log(chat_id=None, target_id=None, limit: int = 20):
    """chat_id=None یعنی همه‌ی گروه‌ها (فقط برای نمای سراسری مالک در پیوی)."""
    with db() as conn:
        c = conn.cursor()
        if chat_id is not None and target_id:
            c.execute(
                "SELECT chat_id, actor_id, actor_name, action, target_id, target_name, "
                "detail, created_at FROM audit_log WHERE chat_id=? AND target_id=? "
                "ORDER BY id DESC LIMIT ?",
                (chat_id, target_id, limit),
            )
        elif chat_id is not None:
            c.execute(
                "SELECT chat_id, actor_id, actor_name, action, target_id, target_name, "
                "detail, created_at FROM audit_log WHERE chat_id=? "
                "ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            )
        else:
            c.execute(
                "SELECT chat_id, actor_id, actor_name, action, target_id, target_name, "
                "detail, created_at FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        return c.fetchall()


def render_audit_log(rows, title: str = "لاگ اقدامات ادمین‌ها",
                     show_chat: bool = False) -> str:
    if not rows:
        return f"{E('info')} هنوز اقدامی در لاگ ثبت نشده."
    lines = [f"{E('audit')} <b>{esc(title)}</b>", DIVIDER]
    for chat_id, actor_id, actor_name, action, target_id, target_name, detail, created in rows:
        icon_key, label = AUDIT_ACTION_LABELS.get(action, ("pin", action))
        if actor_id == SYSTEM_ACTOR_ID:
            actor = f"{E('system')} بات (خودکار)"
        else:
            actor = mention_link(actor_id, actor_name or actor_id) if actor_id else "-"
        line = f"{E(icon_key)} <b>{esc(label)}</b> — {actor}"
        if show_chat:
            line += f" • گروه <code>{chat_id}</code>"
        if target_id:
            line += f" ← {mention_link(target_id, target_name or target_id)}"
        if detail:
            line += f"\n   {E('scroll')} {esc(detail)}"
        line += f"\n   {E('clock')} <code>{esc(str(created)[:16])}</code>"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# کپچای اعضای جدید (ضد بات/اسپمر در ورود)
# ---------------------------------------------------------------------------
CAPTCHA_EMOJI_POOL = [
    "🍎", "🍌", "🍇", "🍒", "🍉", "🍋", "🍓", "🍍",
    "⚽", "🏀", "🎈", "🎲", "🚗", "✈️", "🎸", "⭐",
]
CAPTCHA_CHOICES = 4        # تعداد دکمه‌های نمایش‌داده‌شده
CAPTCHA_MAX_ATTEMPTS = 3


def create_captcha(chat_id: int, user_id: int, message_id: int, minutes: int) -> str:
    correct = random.choice(CAPTCHA_EMOJI_POOL)
    expires = _utcnow() + timedelta(minutes=minutes)
    with db(commit=True) as conn:
        conn.execute(
            """
            INSERT INTO captcha_pending (chat_id, user_id, correct_emoji, message_id,
                                         attempts, expires_at)
            VALUES (?, ?, ?, ?, 0, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                correct_emoji=excluded.correct_emoji,
                message_id=excluded.message_id,
                attempts=0,
                expires_at=excluded.expires_at,
                created_at=CURRENT_TIMESTAMP
            """,
            (chat_id, user_id, correct, message_id,
             expires.astimezone(timezone.utc).isoformat()),
        )
    return correct


def get_captcha(chat_id: int, user_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT correct_emoji, message_id, attempts, expires_at "
            "FROM captcha_pending WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
        return c.fetchone()


def bump_captcha_attempts(chat_id: int, user_id: int) -> int:
    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE captcha_pending SET attempts = attempts + 1 "
            "WHERE chat_id=? AND user_id=?", (chat_id, user_id),
        )
        c.execute("SELECT attempts FROM captcha_pending WHERE chat_id=? AND user_id=?",
                  (chat_id, user_id))
        row = c.fetchone()
        return row[0] if row else CAPTCHA_MAX_ATTEMPTS


def delete_captcha(chat_id: int, user_id: int):
    with db(commit=True) as conn:
        conn.execute("DELETE FROM captcha_pending WHERE chat_id=? AND user_id=?",
                     (chat_id, user_id))


def list_expired_captchas():
    """(chat_id, user_id, message_id) برای کپچاهایی که مهلتشان تمام شده."""
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT chat_id, user_id, message_id, expires_at FROM captcha_pending")
        rows = c.fetchall()
    now = _utcnow()
    expired = []
    for chat_id, user_id, message_id, expires_at in rows:
        dt = _parse_dt(expires_at)
        if dt is None or now >= dt:
            expired.append((chat_id, user_id, message_id))
    return expired


def captcha_keyboard(chat_id: int, user_id: int, correct: str):
    options = {correct}
    pool = [e for e in CAPTCHA_EMOJI_POOL if e != correct]
    while len(options) < CAPTCHA_CHOICES and pool:
        options.add(pool.pop(random.randrange(len(pool))))
    options = list(options)
    random.shuffle(options)
    row = [InlineKeyboardButton(e, callback_data=f"cap:{user_id}:{e}") for e in options]
    # حداکثر ۴ دکمه در یک ردیف تا در موبایل هم زشت نشود
    rows = [row[i:i + 4] for i in range(0, len(row), 4)]
    return InlineKeyboardMarkup(rows)


def add_warning(chat_id: int, user_id: int, reason: str = "") -> int:
    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute("SELECT count FROM user_warnings WHERE chat_id=? AND user_id=?",
                  (chat_id, user_id))
        row = c.fetchone()
        new_count = (row[0] if row else 0) + 1
        c.execute(
            """
            INSERT INTO user_warnings (chat_id, user_id, count, last_reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                count=excluded.count,
                last_reason=excluded.last_reason,
                updated_at=CURRENT_TIMESTAMP
            """,
            (chat_id, user_id, new_count, reason),
        )
    return new_count


def remove_warning(chat_id: int, user_id: int) -> int:
    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute("SELECT count FROM user_warnings WHERE chat_id=? AND user_id=?",
                  (chat_id, user_id))
        row = c.fetchone()
        new_count = max(0, (row[0] if row else 0) - 1)
        if new_count == 0:
            c.execute("DELETE FROM user_warnings WHERE chat_id=? AND user_id=?",
                      (chat_id, user_id))
        else:
            c.execute("UPDATE user_warnings SET count=?, updated_at=CURRENT_TIMESTAMP "
                      "WHERE chat_id=? AND user_id=?", (new_count, chat_id, user_id))
    return new_count


def get_warnings(chat_id: int, user_id: int) -> int:
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT count FROM user_warnings WHERE chat_id=? AND user_id=?",
                  (chat_id, user_id))
        row = c.fetchone()
    return row[0] if row else 0


def reset_warnings(chat_id: int, user_id: int):
    with db(commit=True) as conn:
        conn.execute("DELETE FROM user_warnings WHERE chat_id=? AND user_id=?",
                     (chat_id, user_id))


def log_message(chat_id: int, user_id: int):
    with db(commit=True) as conn:
        conn.execute("INSERT INTO message_log (chat_id, user_id, sent_at) VALUES (?, ?, ?)",
                     (chat_id, user_id, time.time()))


def count_recent_messages(chat_id: int, user_id: int, window_seconds: int) -> int:
    with db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM message_log WHERE chat_id=? AND user_id=? AND sent_at>=?",
            (chat_id, user_id, time.time() - window_seconds),
        )
        return c.fetchone()[0]


def log_and_count(chat_id: int, user_id: int, window_seconds: int) -> int:
    """ثبت پیام و شمارش در یک کانکشن — برای اینکه ضد اسپم روی هر پیام دوبار DB باز نکند."""
    now = time.time()
    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO message_log (chat_id, user_id, sent_at) VALUES (?, ?, ?)",
                  (chat_id, user_id, now))
        c.execute(
            "SELECT COUNT(*) FROM message_log WHERE chat_id=? AND user_id=? AND sent_at>=?",
            (chat_id, user_id, now - window_seconds),
        )
        return c.fetchone()[0]


def clear_message_log(chat_id: int, user_id: int):
    with db(commit=True) as conn:
        conn.execute("DELETE FROM message_log WHERE chat_id=? AND user_id=?",
                     (chat_id, user_id))


def cleanup_old_message_logs(older_than_seconds: int = 3600):
    """لاگ‌های قدیمی و سایلنت‌های منقضی را پاک می‌کند (مقایسه‌ی درستِ تاریخ)."""
    try:
        with db(commit=True) as conn:
            conn.execute("DELETE FROM message_log WHERE sent_at < ?",
                         (time.time() - older_than_seconds,))
            c = conn.cursor()
            c.execute("SELECT chat_id, user_id, until FROM muted_users")
            now = _utcnow()
            expired = []
            for cid, uid, until in c.fetchall():
                dt = _parse_dt(until)
                if dt is None or now >= dt:
                    expired.append((cid, uid))
            for cid, uid in expired:
                conn.execute("DELETE FROM muted_users WHERE chat_id=? AND user_id=?",
                             (cid, uid))
            if expired:
                logger.info(f"{len(expired)} سایلنت منقضی پاک شد.")
    except Exception as e:
        logger.warning(f"پاکسازی لاگ ناموفق: {e}")


def mute_user(chat_id: int, user_id: int, until: datetime, reason: str = ""):
    if until.tzinfo is None:
        until = until.astimezone()
    with db(commit=True) as conn:
        conn.execute(
            """
            INSERT INTO muted_users (chat_id, user_id, until, reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                until=excluded.until, reason=excluded.reason
            """,
            (chat_id, user_id, until.astimezone(timezone.utc).isoformat(), reason),
        )
    _muted_cache.pop((chat_id, user_id), None)


def unmute_user(chat_id: int, user_id: int):
    with db(commit=True) as conn:
        conn.execute("DELETE FROM muted_users WHERE chat_id=? AND user_id=?",
                     (chat_id, user_id))
    _muted_cache.pop((chat_id, user_id), None)


# کش کوتاه وضعیت سایلنت تا روی هر پیام به دیتابیس نزنیم
_muted_cache: dict = {}          # (chat_id, user_id) -> (checked_at, until|None)
MUTED_CACHE_TTL = 15


def is_muted(chat_id: int, user_id: int) -> bool:
    key = (chat_id, user_id)
    now = _utcnow()
    cached = _muted_cache.get(key)
    if cached and time.time() - cached[0] < MUTED_CACHE_TTL:
        until = cached[1]
        if until is None:
            return False
        if now < until:
            return True
        unmute_user(chat_id, user_id)
        return False

    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT until FROM muted_users WHERE chat_id=? AND user_id=?",
                  (chat_id, user_id))
        row = c.fetchone()

    until = _parse_dt(row[0]) if row else None
    if row and until is None:          # رکورد خراب
        unmute_user(chat_id, user_id)
        return False
    if until and now >= until:
        unmute_user(chat_id, user_id)
        return False
    _muted_cache[key] = (time.time(), until)
    return bool(until)


def get_mute_until(chat_id: int, user_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT until, reason FROM muted_users WHERE chat_id=? AND user_id=?",
                  (chat_id, user_id))
        row = c.fetchone()
    if not row:
        return None, None
    return _parse_dt(row[0]), row[1]


def list_muted(chat_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, until, reason FROM muted_users WHERE chat_id=?",
                  (chat_id,))
        return c.fetchall()


# ---------------------------------------------------------------------------
# قرعه‌کشی — لایه دیتابیس
# ---------------------------------------------------------------------------
def get_group_range(chat_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT min_num, max_num FROM group_settings WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
    return (row[0], row[1]) if row else (DEFAULT_MIN_NUM, DEFAULT_MAX_NUM)


def set_group_range(chat_id: int, mn: int, mx: int):
    with db(commit=True) as conn:
        conn.execute(
            "INSERT INTO group_settings (chat_id, min_num, max_num) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET min_num=excluded.min_num, "
            "max_num=excluded.max_num",
            (chat_id, mn, mx),
        )


def get_open_lottery(chat_id):
    with db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, status, min_num, max_num FROM lotteries "
            "WHERE chat_id=? AND status IN ('active','closed') ORDER BY id DESC LIMIT 1",
            (chat_id,),
        )
        return c.fetchone()


def get_latest_lottery(chat_id):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, status FROM lotteries WHERE chat_id=? ORDER BY id DESC LIMIT 1",
                  (chat_id,))
        return c.fetchone()


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


_db_admin_cache = {"time": 0.0, "ids": set()}
DB_ADMIN_CACHE_TTL = 60


def get_db_admin_ids() -> set:
    if time.time() - _db_admin_cache["time"] < DB_ADMIN_CACHE_TTL:
        return _db_admin_cache["ids"]
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM admins")
        ids = {row[0] for row in c.fetchall()}
    _db_admin_cache.update({"time": time.time(), "ids": ids})
    return ids


def add_admin_db(user_id: int, username, full_name, added_by: int):
    with db(commit=True) as conn:
        conn.execute(
            "INSERT INTO admins (user_id, username, full_name, added_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
            "full_name=excluded.full_name",
            (user_id, username, full_name, added_by),
        )
    _db_admin_cache["time"] = 0.0


def remove_admin_db(user_id: int) -> bool:
    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        removed = c.rowcount > 0
    _db_admin_cache["time"] = 0.0
    return removed


def list_db_admins():
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, username, full_name, added_at FROM admins ORDER BY added_at")
        return c.fetchall()


_known_chat_seen: dict = {}


def track_known_chat(chat) -> None:
    if not chat or chat.type not in GROUP_TYPES:
        return
    # حداکثر هر ۵ دقیقه یک‌بار برای هر گروه بنویس (کاهش فشار روی دیتابیس)
    now = time.time()
    if now - _known_chat_seen.get(chat.id, 0) < 300:
        return
    _known_chat_seen[chat.id] = now
    try:
        with db(commit=True) as conn:
            conn.execute(
                "INSERT INTO known_chats (chat_id, title, type, last_seen) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, "
                "last_seen=CURRENT_TIMESTAMP",
                (chat.id, chat.title, chat.type),
            )
    except Exception as e:
        logger.warning(f"ثبت گروه ناموفق: {e}")


# ---------------------------------------------------------------------------
# دسترسی‌ها
# ---------------------------------------------------------------------------
_admin_cache: dict = {}          # chat_id -> (timestamp, set(user_ids))
ADMIN_CACHE_TTL = 300


async def get_chat_admin_ids(bot, chat_id: int) -> set:
    """لیست ادمین‌های گروه با کش ۵ دقیقه‌ای (برای نزدن اسپم به API تلگرام)."""
    now = time.time()
    cached = _admin_cache.get(chat_id)
    if cached and now - cached[0] < ADMIN_CACHE_TTL:
        return cached[1]
    try:
        members = await bot.get_chat_administrators(chat_id)
        ids = {m.user.id for m in members}
    except TelegramError as e:
        logger.debug(f"گرفتن ادمین‌های {chat_id} ناموفق: {e}")
        ids = cached[1] if cached else set()
    _admin_cache[chat_id] = (now, ids)
    return ids


def invalidate_admin_cache(chat_id: int):
    _admin_cache.pop(chat_id, None)


async def is_admin_of(bot, user_id: int, chat_id: int | None) -> bool:
    """مالک/ادمین دیتابیس همیشه، یا ادمین واقعیِ همان گروه."""
    if is_owner(user_id):
        return True
    if user_id in get_db_admin_ids():
        return True
    if chat_id is None:
        return False
    return user_id in await get_chat_admin_ids(bot, chat_id)


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return False
    chat_id = chat.id if chat and chat.type in GROUP_TYPES else None
    return await is_admin_of(context.bot, user.id, chat_id)


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        msg: str | None = None) -> bool:
    if await is_admin(update, context):
        return True
    await reply(update, msg or f"{E('cross')} این دستور فقط برای ادمین‌هاست.")
    return False


async def require_group(update: Update) -> bool:
    if update.effective_chat.type in GROUP_TYPES:
        return True
    await reply(update, f"{E('info')} این دستور را باید توی گروه بزنی.")
    return False


async def bot_rights(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> dict:
    """دسترسی‌های خود بات در گروه — برای پیام‌های تشخیص عیب."""
    info = {"admin": False, "delete": False, "restrict": False,
            "pin": False, "invite": False, "error": None}
    try:
        me = await context.bot.get_chat_member(chat_id, context.bot.id)
        info["admin"] = me.status in (ChatMemberStatus.ADMINISTRATOR,
                                      ChatMemberStatus.OWNER)
        if me.status == ChatMemberStatus.OWNER:
            info.update({"delete": True, "restrict": True, "pin": True, "invite": True})
        else:
            info["delete"] = bool(getattr(me, "can_delete_messages", False))
            info["restrict"] = bool(getattr(me, "can_restrict_members", False))
            info["pin"] = bool(getattr(me, "can_pin_messages", False))
            info["invite"] = bool(getattr(me, "can_invite_users", False))
    except TelegramError as e:
        info["error"] = str(e)
        logger.warning(f"گرفتن وضعیت بات در {chat_id} ناموفق: {e}")
    return info


async def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """در گروه: همان گروه. در پیوی: آیدی گروه باید آرگومان اول باشد."""
    chat = update.effective_chat
    if chat.type != "private":
        return chat.id, list(context.args or []), None
    args = list(context.args or [])
    if not args:
        msg = update.effective_message
        raw = (msg.text or "") if msg else ""
        cmd_name = raw.split()[0].lstrip("/").split("@")[0] if raw.startswith("/") else "command"
        return (
            None, [],
            f"{E('info')} توی پیوی باید آیدی گروه هدف را هم بدهی.\n"
            f"مثال: <code>/{cmd_name} -1001234567890</code>\n"
            f"یا از پنل دکمه‌ای: /menu — لیست گروه‌ها: /groups",
        )
    try:
        target_chat_id = int(args[0])
    except ValueError:
        return None, [], f"{E('cross')} آیدی گروه نامعتبر است. با /groups لیست را ببین."
    return target_chat_id, args[1:], None


def mention_of(username, full_name):
    if username:
        return f"@{esc(username)}"
    return f"<b>{esc(full_name or 'کاربر')}</b>"


def mention_link(user_id: int, name) -> str:
    """منشن کلیک‌پذیر حتی بدون یوزرنیم."""
    return f'<a href="tg://user?id={user_id}">{esc(name or user_id)}</a>'


def medal_for(rank: int) -> str:
    return {1: E("medal1"), 2: E("medal2"), 3: E("medal3")}.get(rank, f"{rank}.")


def human_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} دقیقه"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ساعت" + (f" و {mins} دقیقه" if mins else "")
    days, hours = divmod(hours, 24)
    return f"{days} روز" + (f" و {hours} ساعت" if hours else "")


# ---------------------------------------------------------------------------
# منطق اصلی قرعه‌کشی
# ---------------------------------------------------------------------------
def core_start_lottery(chat_id: int):
    if get_open_lottery(chat_id):
        return False, (
            f"{E('warn')} یک قرعه‌کشی از قبل باز یا بسته است و هنوز برنده مشخص نشده.\n"
            f"اول برنده را مشخص کن یا با /cancellottery لغوش کن."
        )
    mn, mx = get_group_range(chat_id)
    with db(commit=True) as conn:
        conn.execute(
            "INSERT INTO lotteries (chat_id, status, min_num, max_num) VALUES (?, 'active', ?, ?)",
            (chat_id, mn, mx),
        )
    text = (
        f"{E('party')} <b>قرعه‌کشی شروع شد!</b>\n"
        f"{DIVIDER}\n"
        f"{E('pin')} یک عدد بین <code>{mn}</code> تا <code>{mx}</code> بفرست تا ثبت شود\n"
        f"{E('info')} هر عدد فقط یک‌بار قابل انتخاب است\n"
        f"{E('info')} هر نفر فقط یک عدد می‌تواند ثبت کند\n"
        f"{DIVIDER}\n"
        f"پایان ثبت‌نام: /stoplottery • وضعیت: /status"
    )
    return True, text


def core_stop_lottery(chat_id: int):
    row = get_open_lottery(chat_id)
    if not row or row[1] != "active":
        return False, f"{E('warn')} قرعه‌کشی فعالی برای بستن وجود ندارد."
    lottery_id = row[0]
    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute("UPDATE lotteries SET status='closed', closed_at=CURRENT_TIMESTAMP WHERE id=?",
                  (lottery_id,))
        c.execute("SELECT COUNT(*) FROM entries WHERE lottery_id=?", (lottery_id,))
        count = c.fetchone()[0]
    text = (
        f"{E('lock')} <b>ثبت‌نام بسته شد</b>\n"
        f"{E('chart')} تعداد شرکت‌کننده: <code>{count}</code> نفر\n\n"
        f"برنده به‌زودی مشخص می‌شود."
    )
    return True, text


def core_cancel_lottery(chat_id: int):
    """لغو قرعه‌کشی باز یا بسته (بدون تعیین برنده)."""
    row = get_open_lottery(chat_id)
    if not row:
        return False, f"{E('info')} قرعه‌کشی بازی برای لغو وجود ندارد."
    lottery_id = row[0]
    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM entries WHERE lottery_id=?", (lottery_id,))
        c.execute("UPDATE lotteries SET status='cancelled', finished_at=CURRENT_TIMESTAMP "
                  "WHERE id=?", (lottery_id,))
    return True, (f"{E('trash')} قرعه‌کشی <code>#{lottery_id}</code> لغو شد و "
                  f"ثبت‌نام‌هایش پاک شد.")


def core_pick_winner(chat_id: int, winners_count: int = 3):
    row = get_open_lottery(chat_id)
    if not row:
        return False, f"{E('warn')} هیچ قرعه‌کشی فعال یا بسته‌ای پیدا نشد."
    lottery_id, status_val = row[0], row[1]
    if status_val == "active":
        return False, f"{E('hourglass')} اول ثبت‌نام را ببند، بعد برنده را مشخص کن."

    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, username, full_name, number FROM entries WHERE lottery_id=?",
                  (lottery_id,))
        entries = c.fetchall()
        if not entries:
            return False, f"{E('warn')} هیچ شرکت‌کننده‌ای ثبت نشده بود."

        capped_note = ""
        if winners_count > len(entries):
            capped_note = (
                f"{E('info')} فقط <code>{len(entries)}</code> شرکت‌کننده بود، "
                f"پس همه‌شان برنده شدند."
            )
        winners_count = max(1, min(winners_count, len(entries)))
        chosen = random.sample(entries, winners_count)

        for rank, (user_id, username, full_name, number) in enumerate(chosen, start=1):
            c.execute(
                "INSERT INTO winners (lottery_id, rank, user_id, username, full_name, number) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (lottery_id, rank, user_id, username, full_name, number),
            )
        c.execute("UPDATE lotteries SET status='finished', finished_at=CURRENT_TIMESTAMP WHERE id=?",
                  (lottery_id,))

    lines = [f"{E('trophy')} <b>نتیجه قرعه‌کشی</b>", DIVIDER]
    for rank, (user_id, username, full_name, number) in enumerate(chosen, start=1):
        lines.append(
            f"{medal_for(rank)} عدد <code>{number}</code> — "
            f"{mention_link(user_id, full_name or username or user_id)}"
        )
    lines.append(DIVIDER)
    lines.append(f"{E('gift')} تبریک می‌گوییم! {E('love')}")
    if capped_note:
        lines.append(capped_note)
    return True, "\n".join(lines)


def core_status_text(chat_id: int) -> str:
    row = get_open_lottery(chat_id)
    if not row:
        mn, mx = get_group_range(chat_id)
        return (
            f"{E('info')} قرعه‌کشی فعالی وجود ندارد.\n"
            f"{E('range')} بازه‌ی عدد برای قرعه‌کشی بعدی: <code>{mn}</code> تا <code>{mx}</code>"
        )
    lottery_id, status_val, mn, mx = row
    mn = mn if mn is not None else DEFAULT_MIN_NUM
    mx = mx if mx is not None else DEFAULT_MAX_NUM
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT number FROM entries WHERE lottery_id=? ORDER BY number", (lottery_id,))
        numbers = [str(r[0]) for r in c.fetchall()]
    status_fa = {
        "active": f"{E('dice')} باز (در حال ثبت‌نام)",
        "closed": f"{E('lock')} بسته (منتظر برنده)",
    }.get(status_val, esc(status_val))
    text = (
        f"{E('chart')} <b>وضعیت قرعه‌کشی</b>\n"
        f"{DIVIDER}\n"
        f"دوره: <code>#{lottery_id}</code>\n"
        f"وضعیت: {status_fa}\n"
        f"بازه‌ی مجاز: <code>{mn}</code> تا <code>{mx}</code>\n"
        f"شرکت‌کننده: <code>{len(numbers)}</code> نفر\n"
    )
    if numbers:
        shown = numbers[:200]
        text += f"\n{E('pin')} اعداد گرفته‌شده:\n<code>{'، '.join(shown)}</code>"
        if len(numbers) > len(shown):
            text += f"\n{E('info')} و <code>{len(numbers) - len(shown)}</code> عدد دیگر…"
    return text


def core_my_entry_text(chat_id: int, user_id: int) -> str:
    row = get_open_lottery(chat_id)
    if not row:
        return f"{E('info')} قرعه‌کشی فعالی وجود ندارد."
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT number FROM entries WHERE lottery_id=? AND user_id=?",
                  (row[0], user_id))
        r = c.fetchone()
    if r:
        return f"{E('id')} عدد ثبت‌شده‌ی شما: <code>{r[0]}</code>"
    return f"{E('info')} شما هنوز عددی ثبت نکرده‌اید."


def core_history_text(chat_id: int) -> str:
    with db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, status, created_at FROM lotteries WHERE chat_id=? ORDER BY id DESC LIMIT 10",
            (chat_id,),
        )
        lotteries = c.fetchall()
        if not lotteries:
            return f"{E('info')} هنوز هیچ قرعه‌کشی‌ای توی این گروه برگزار نشده."

        lines = [f"{E('scroll')} <b>تاریخچه قرعه‌کشی‌ها</b> (تا ۱۰ مورد آخر)", DIVIDER]
        status_fa_map = {
            "active": "در حال ثبت‌نام",
            "closed": "بسته/منتظر برنده",
            "finished": "پایان‌یافته",
            "cancelled": "لغو شده",
        }
        for lottery_id, status_val, created_at in lotteries:
            c.execute("SELECT COUNT(*) FROM entries WHERE lottery_id=?", (lottery_id,))
            participant_count = c.fetchone()[0]
            c.execute(
                "SELECT rank, number, username, full_name FROM winners "
                "WHERE lottery_id=? ORDER BY rank",
                (lottery_id,),
            )
            winners = c.fetchall()
            date_str = str(created_at).split(".")[0] if created_at else "-"
            lines.append(
                f"{E('pin')} <b>دوره #{lottery_id}</b> — {esc(date_str)}\n"
                f"وضعیت: {esc(status_fa_map.get(status_val, status_val))} • "
                f"شرکت‌کننده: <code>{participant_count}</code>"
            )
            for rank, number, username, full_name in winners:
                lines.append(
                    f"   {medal_for(rank)} عدد <code>{number}</code> — "
                    f"{mention_of(username, full_name)}"
                )
            lines.append(DIVIDER)
    return "\n".join(lines)


def core_leaderboard_text(chat_id: int) -> str:
    with db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT w.user_id, w.username, w.full_name, COUNT(*) AS wins "
            "FROM winners w JOIN lotteries l ON w.lottery_id = l.id "
            "WHERE l.chat_id=? GROUP BY w.user_id "
            "ORDER BY wins DESC, MIN(w.rank) ASC LIMIT 10",
            (chat_id,),
        )
        rows = c.fetchall()
    if not rows:
        return f"{E('info')} هنوز هیچ برنده‌ای توی این گروه ثبت نشده."
    lines = [f"{E('leaderboard')} <b>جدول برترین‌های قرعه‌کشی</b>", DIVIDER]
    for idx, (user_id, username, full_name, wins) in enumerate(rows, start=1):
        lines.append(
            f"{medal_for(idx)} {mention_of(username, full_name)} — "
            f"<code>{wins}</code> بار برنده"
        )
    lines.append(DIVIDER)
    return "\n".join(lines)


def core_groups_list():
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT chat_id, title FROM known_chats ORDER BY last_seen DESC LIMIT 25")
        return c.fetchall()


def core_build_export_workbook(chat_id: int):
    try:
        import openpyxl
        from openpyxl.styles import Font
    except ImportError:
        return "NO_OPENPYXL", None

    row = get_latest_lottery(chat_id)
    if not row:
        return None, None
    lottery_id = row[0]

    with db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT number, username, full_name, user_id, created_at FROM entries "
            "WHERE lottery_id=? ORDER BY number",
            (lottery_id,),
        )
        entries = c.fetchall()
        c.execute(
            "SELECT rank, number, username, full_name FROM winners "
            "WHERE lottery_id=? ORDER BY rank",
            (lottery_id,),
        )
        winners = c.fetchall()

    if not entries:
        return None, None

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "شرکت‌کنندگان"
    ws.append(["عدد", "یوزرنیم", "نام", "شناسه کاربری", "زمان ثبت"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for number, username, full_name, user_id, created_at in entries:
        ws.append([number, f"@{username}" if username else "", full_name,
                   user_id, str(created_at or "")])

    if winners:
        ws2 = wb.create_sheet("برندگان")
        ws2.append(["رتبه", "عدد", "یوزرنیم", "نام"])
        for cell in ws2[1]:
            cell.font = Font(bold=True)
        for rank, number, username, full_name in winners:
            ws2.append([rank, number, f"@{username}" if username else "", full_name])

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        wb.save(tmp.name)
        tmp_path = tmp.name
    return tmp_path, lottery_id


# ---------------------------------------------------------------------------
# چک آی‌پی / دامنه
# ---------------------------------------------------------------------------
HTTP_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
}

IP_GEO_API = ("http://ip-api.com/json/{target}"
              "?fields=status,message,country,countryCode,regionName,city,as,query")
CHECK_HOST_NODES_URL = "https://check-host.net/nodes/hosts"
CHECK_HOST_PING_URL = "https://check-host.net/check-ping"
CHECK_HOST_RESULT_URL = "https://check-host.net/check-result/{request_id}"

CHECK_HOST_COUNTRY_ORDER = [
    ("ch", "Switzerland"), ("de", "Germany"), ("es", "Spain"), ("fi", "Finland"),
    ("fr", "France"), ("gb", "UK"), ("ir", None), ("it", "Italy"),
    ("nl", "Netherlands"), ("pl", "Poland"), ("se", "Sweden"),
    ("tr", "Turkey"), ("us", "USA"),
]
CHECK_HOST_IRAN_CITIES = ["Esfahan", "Mashhad", "Qom", "Shiraz", "Tabriz", "Tehran"]

_check_host_nodes_cache = {"time": 0.0, "data": {}}
_CHECK_HOST_NODES_TTL = 3600
_ip_geo_cache: dict = {}
_IP_GEO_TTL = 300
_check_last_used: dict = {}
_CHECK_COOLDOWN_SECONDS = 30


def _looks_like_ip_or_domain(text: str) -> bool:
    text = (text or "").strip()
    if not text or " " in text or len(text) > 253:
        return False
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        pass
    return bool(re.match(r"^[A-Za-z0-9]([A-Za-z0-9\-\.]{0,251})\.[A-Za-z]{2,}$", text))


def _check_cooldown_remaining(user_id: int) -> float:
    last = _check_last_used.get(user_id)
    if last is None:
        return 0.0
    return max(0.0, _CHECK_COOLDOWN_SECONDS - (time.time() - last))


async def _ch_fetch_nodes(client: httpx.AsyncClient) -> dict:
    now = time.time()
    if _check_host_nodes_cache["data"] and \
            now - _check_host_nodes_cache["time"] < _CHECK_HOST_NODES_TTL:
        return _check_host_nodes_cache["data"]
    resp = await client.get(CHECK_HOST_NODES_URL, headers=HTTP_HEADERS, timeout=15)
    resp.raise_for_status()
    nodes = resp.json().get("nodes", {})
    _check_host_nodes_cache.update({"data": nodes, "time": now})
    return nodes


def _ch_select_nodes(nodes: dict):
    by_country: dict = {}
    for node_name, info in nodes.items():
        loc = (info or {}).get("location") or []
        if not loc:
            continue
        by_country.setdefault(str(loc[0]).lower(), []).append((node_name, loc))

    selected = []
    for code, label in CHECK_HOST_COUNTRY_ORDER:
        candidates = sorted(by_country.get(code, []), key=lambda x: x[0])
        if code != "ir":
            if candidates:
                selected.append((candidates[0][0], code, label))
            continue
        used_cities = set()
        tehran_count = 0
        for node_name, loc in candidates:
            city = str(loc[2]).strip() if len(loc) > 2 and loc[2] else ""
            match = next((x for x in CHECK_HOST_IRAN_CITIES if x.lower() == city.lower()), None)
            if not match:
                continue
            if match == "Tehran":
                if tehran_count >= 2:
                    continue
                tehran_count += 1
                selected.append((node_name, code, f"Tehran {tehran_count}"))
            else:
                if match in used_cities:
                    continue
                used_cities.add(match)
                selected.append((node_name, code, match))
    return selected


async def _ch_run_ping_check(client: httpx.AsyncClient, target: str, node_names: list):
    params = [("host", target)] + [("node", n) for n in node_names]
    resp = await client.get(CHECK_HOST_PING_URL, params=params,
                            headers=HTTP_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError("check-host.net درخواست را قبول نکرد.")
    return data["request_id"]


async def _ch_poll_results(client: httpx.AsyncClient, request_id: str,
                           attempts: int = 6, delay: float = 2.0):
    result = {}
    for _ in range(attempts):
        await asyncio.sleep(delay)
        resp = await client.get(CHECK_HOST_RESULT_URL.format(request_id=request_id),
                                headers=HTTP_HEADERS, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if not any(v is None for v in result.values()):
            break
    return result


def _format_ping_line(country_code: str, label: str, node_result) -> str:
    flag = flag_emoji(country_code)
    attempts = []
    if node_result and isinstance(node_result, list) and node_result[0] is not None:
        attempts = node_result[0]
    if not attempts:
        return f"{flag} {esc(label)} ➤ <code>timeout</code> [✗ ✗ ✗ ✗]"
    marks, ok_times = [], []
    for a in attempts[:4]:
        if isinstance(a, (list, tuple)) and len(a) >= 2 and a[0] == "OK":
            marks.append("✓")
            ok_times.append(a[1])
        else:
            marks.append("✗")
    while len(marks) < 4:
        marks.append("✗")
    time_str = f"{(sum(ok_times) / len(ok_times)) * 1000:.1f} ms" if ok_times else "timeout"
    return f"{flag} {esc(label)} ➤ <code>{time_str}</code> [{' '.join(marks)}]"


async def _get_ip_geo(client: httpx.AsyncClient, target: str) -> dict:
    now = time.time()
    cached = _ip_geo_cache.get(target)
    if cached and now - cached[0] < _IP_GEO_TTL:
        return cached[1]
    try:
        geo_resp = await client.get(IP_GEO_API.format(target=target), timeout=10)
        geo = geo_resp.json()
    except Exception as e:
        logger.warning(f"خطا در دریافت اطلاعات لوکیشن: {e}")
        geo = {"status": "fail"}
    _ip_geo_cache[target] = (now, geo)
    return geo


async def core_check_ip(target: str):
    target = (target or "").strip()
    if not _looks_like_ip_or_domain(target):
        return False, (f"{E('cross')} این یک آی‌پی یا دامنه‌ی معتبر نیست.\n"
                       f"مثال: <code>/check 8.8.8.8</code>")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        geo = await _get_ip_geo(client, target)

        if geo.get("status") != "success":
            geo_lines = [f"{E('warn')} اطلاعات لوکیشن این هدف پیدا نشد."]
        else:
            country_code = (geo.get("countryCode") or "").upper()
            flag = flag_emoji(country_code) if country_code else "🏳️"
            geo_lines = [
                f"{E('network')} Address: <code>{esc(target)}</code>",
                f"{E('globe')} IP: <code>{esc(geo.get('query', target))}</code>",
                f"{flag} Country: {esc(geo.get('country', '-'))}",
                f"{E('location')} Region: {esc(geo.get('regionName', '-'))}",
                f"{E('city')} City: {esc(geo.get('city', '-'))}",
                f"{E('office')} ASN: {esc(geo.get('as', '-'))}",
            ]

        try:
            nodes = await _ch_fetch_nodes(client)
            selected = _ch_select_nodes(nodes)
        except Exception as e:
            logger.warning(f"خطا در دریافت نودهای check-host: {e}")
            return False, ("\n".join(geo_lines) +
                           f"\n\n{E('warn')} دریافت سرورهای پینگ ناموفق بود.")

        if not selected:
            return False, ("\n".join(geo_lines) +
                           f"\n\n{E('warn')} هیچ نود مناسبی برای پینگ پیدا نشد.")

        node_names = [n for n, _, _ in selected]
        try:
            request_id = await _ch_run_ping_check(client, target, node_names)
            result = await _ch_poll_results(client, request_id)
        except Exception as e:
            logger.warning(f"خطا در اجرای چک پینگ: {e}")
            return False, ("\n".join(geo_lines) +
                           f"\n\n{E('warn')} اجرای تست پینگ ناموفق بود.")

    ping_lines = [_format_ping_line(code, label, result.get(node_name))
                  for node_name, code, label in selected]
    return True, "\n".join(geo_lines) + "\n\n" + "\n".join(ping_lines)


CHECK_TRIGGER_REGEX = re.compile(r"^check\s*!\s*(\S+)", re.IGNORECASE)


async def _run_check_flow(update: Update, target: str):
    user_id = update.effective_user.id
    remaining = _check_cooldown_remaining(user_id)
    if remaining > 0:
        await reply(update, f"{E('warn')} لطفاً <code>{remaining:.0f}</code> ثانیه دیگر "
                            f"صبر کن و دوباره امتحان کن.")
        return
    _check_last_used[user_id] = time.time()
    status_msg = await reply(update, f"{E('network')} در حال بررسی "
                                     f"<code>{esc(target)}</code> ...")
    ok, text = await core_check_ip(target)
    if status_msg is None:
        await reply(update, text)
        return
    try:
        await status_msg.edit_text(text, parse_mode="HTML")
    except TelegramError:
        try:
            await status_msg.edit_text(strip_html(text))
        except TelegramError:
            await reply(update, text)


async def handle_check_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    m = CHECK_TRIGGER_REGEX.match(update.message.text.strip())
    if not m:
        return
    await _run_check_flow(update, m.group(1))


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await reply(
            update,
            f"{E('info')} فرمت درست: <code>/check آی‌پی_یا_دامنه</code>\n"
            f"مثال: <code>/check 8.8.8.8</code>\n"
            f"یا راحت‌تر: <code>Check!8.8.8.8</code>",
        )
        return
    await _run_check_flow(update, context.args[0])


# ---------------------------------------------------------------------------
# قیمت بازار: دلار، ارزها و طلا
# ---------------------------------------------------------------------------
PRICE_TIMEOUT = float(os.environ.get("PRICE_TIMEOUT", "12"))
PRICE_CACHE_TTL = int(os.environ.get("PRICE_CACHE_TTL", "60"))
GOLD_URL = os.environ.get("GOLD_URL", "https://api.gold-api.com/price/XAU").strip()

USD_SOURCES = [
    ("tgju-sana",   "https://api.tgju.online/v1/data/sana/json",   "rial"),
    ("accessban",   "https://api.accessban.com/v1/data/sana/json", "rial"),
    ("priceto.day", "https://api.priceto.day/v1/latest/irr/usd",   "rial"),
    ("baha24",      "https://baha24.com/api/v1/price",             "toman"),
]
if os.environ.get("USD_SOURCE_URL", "").strip():
    USD_SOURCES.insert(0, (
        "custom",
        os.environ["USD_SOURCE_URL"].strip(),
        os.environ.get("USD_SOURCE_UNIT", "rial").strip().lower(),
    ))

FX_SOURCES = [
    "https://api.frankfurter.app/latest?from=USD",
    "https://open.er-api.com/v6/latest/USD",
]

CURRENCY_NAMES = {
    "EUR": "یورو", "GBP": "پوند", "AED": "درهم امارات",
    "TRY": "لیر ترکیه", "CAD": "دلار کانادا", "AUD": "دلار استرالیا",
    "CHF": "فرانک سوئیس", "JPY": "ین ژاپن", "CNY": "یوان چین",
    "RUB": "روبل روسیه",
}

_price_cache = {"time": 0.0, "text": None}

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _latin_number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().translate(_PERSIAN_DIGITS)
    s = s.replace(",", "").replace("٬", "").replace("٫", ".").replace(" ", "")
    s = re.sub(r"[^0-9.+-]", "", s)
    if not s or s in {"+", "-", "."}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _format_price(value, decimals=0):
    if value is None:
        return "نامشخص"
    return f"{value:,.{decimals}f}" if decimals else f"{round(value):,}"


def _extract_sana_usd(payload) -> float | None:
    """استخراج دلار از پاسخ‌های مختلف SANA/TGJU."""
    if not isinstance(payload, dict):
        return None

    containers = [payload]
    sana = payload.get("sana")
    if isinstance(sana, dict):
        containers.insert(0, sana)

    for container in containers:
        data = container.get("data") if isinstance(container, dict) else None
        if isinstance(data, list):
            candidates = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                key = " ".join(str(item.get(k, "")) for k in
                               ("name", "title", "key", "symbol", "type", "code")).lower()
                price = _latin_number(item.get("p") or item.get("price") or item.get("value"))
                if price and ("usd" in key or "dollar" in key or "دلار" in key):
                    candidates.append((key, price))
            for key, price in candidates:
                if "sell" in key or "فروش" in key:
                    return price
            if candidates:
                return candidates[0][1]
            if data and isinstance(data[0], dict):
                price = _latin_number(
                    data[0].get("p") or data[0].get("price") or data[0].get("value")
                )
                if price and price > 0:
                    return price

        for key in ("sana_sell_usd", "sana_buy_usd", "sell_usd", "buy_usd",
                    "usd", "dollar", "price_dollar_rl"):
            value = container.get(key) if isinstance(container, dict) else None
            if isinstance(value, dict):
                value = value.get("p") or value.get("price") or value.get("value")
            price = _latin_number(value)
            if price and price > 0:
                return price
    return None


def _extract_generic_usd(payload) -> float | None:
    """جست‌وجوی بازگشتی در JSON برای نرخ دلار (منابع ناشناس)."""
    found: list = []

    def walk(node, hint=""):
        if found:
            return
        if isinstance(node, dict):
            own = " ".join(str(node.get(k, "")) for k in
                           ("name", "title", "key", "symbol", "code",
                            "target", "currency"))
            full = f"{hint} {own}".strip().lower()
            if "usd" in full or "dollar" in full or "دلار" in full:
                for k in ("sell", "price", "value", "p", "amount", "rate", "close", "last"):
                    v = _latin_number(node.get(k))
                    if v and v > 0:
                        found.append(v)
                        return
            for k, v in node.items():
                walk(v, f"{hint} {k}")
        elif isinstance(node, list):
            for item in node:
                walk(item, hint)

    walk(payload)
    return found[0] if found else None


USD_SANITY_MIN = float(os.environ.get("USD_SANITY_MIN", "30000"))
USD_SANITY_MAX = float(os.environ.get("USD_SANITY_MAX", "5000000"))


def _normalize_usd_toman(value: float | None, unit: str) -> float | None:
    """تبدیل به تومان + تشخیص خودکار واحد اشتباه (ریال/تومان)."""
    if not value or value <= 0:
        return None
    primary = value / 10 if unit == "rial" else value
    alternate = value if unit == "rial" else value / 10
    for candidate in (primary, alternate):
        if USD_SANITY_MIN <= candidate <= USD_SANITY_MAX:
            return candidate
    return None


async def _fetch_json(client: httpx.AsyncClient, url: str):
    response = await client.get(url, headers=HTTP_HEADERS, timeout=PRICE_TIMEOUT)
    response.raise_for_status()
    return response.json()


async def _fetch_usd_toman(client: httpx.AsyncClient):
    """-> (قیمت تومان، نام منبع، لیست خطاها)"""
    errors = []
    for name, url, unit in USD_SOURCES:
        try:
            payload = await _fetch_json(client, url)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}")
            continue
        raw = _extract_sana_usd(payload) or _extract_generic_usd(payload)
        price = _normalize_usd_toman(raw, unit)
        if price:
            return price, name, errors
        errors.append(f"{name}: پاسخ نامعتبر")

    manual = _latin_number(os.environ.get("MARKET_USD_TOMAN"))
    if manual and manual > 0:
        return manual, "MARKET_USD_TOMAN", errors
    manual_rial = _latin_number(os.environ.get("MARKET_USD_IRR"))
    if manual_rial and manual_rial > 0:
        return manual_rial / 10, "MARKET_USD_IRR", errors
    return None, None, errors


async def _fetch_fx_rates(client: httpx.AsyncClient) -> dict:
    for url in FX_SOURCES:
        try:
            data = await _fetch_json(client, url)
        except Exception as e:
            logger.warning(f"منبع نرخ ارز {url} ناموفق: {e}")
            continue
        if isinstance(data, dict) and isinstance(data.get("rates"), dict):
            return data["rates"]
    return {}


async def _fetch_gold_usd(client: httpx.AsyncClient) -> float | None:
    manual = _latin_number(os.environ.get("MARKET_GOLD_USD"))
    if manual and manual > 0:
        return manual
    try:
        data = await _fetch_json(client, GOLD_URL)
    except Exception as e:
        logger.warning(f"دریافت انس طلا ناموفق: {e}")
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("price") or data.get("value")
    if raw is None and isinstance(data.get("data"), dict):
        raw = data["data"].get("price") or data["data"].get("value")
    return _latin_number(raw)


async def _build_market_prices(force: bool = False) -> str:
    """هیچ‌وقت exception نمی‌دهد — همیشه یک متن قابل نمایش برمی‌گرداند."""
    now = time.time()
    if not force and _price_cache["text"] and now - _price_cache["time"] < PRICE_CACHE_TTL:
        return _price_cache["text"]

    async with httpx.AsyncClient(follow_redirects=True) as client:
        usd_result, fx_rates, gold_usd_oz = await asyncio.gather(
            _fetch_usd_toman(client),
            _fetch_fx_rates(client),
            _fetch_gold_usd(client),
            return_exceptions=True,
        )

    if isinstance(usd_result, BaseException):
        usd_toman, usd_source, usd_errors = None, None, [type(usd_result).__name__]
    else:
        usd_toman, usd_source, usd_errors = usd_result
    if isinstance(fx_rates, BaseException) or not isinstance(fx_rates, dict):
        fx_rates = {}
    if isinstance(gold_usd_oz, BaseException):
        gold_usd_oz = None

    lines = [f"{E('chart')} <b>قیمت بازار</b>", DIVIDER]

    if usd_toman:
        lines.append(f"{E('usd')} دلار: <code>{_format_price(usd_toman)}</code> تومان")
        added = 0
        for code, fa in CURRENCY_NAMES.items():
            rate = _latin_number(fx_rates.get(code))
            if rate and rate > 0:
                lines.append(f"{E('currency')} {fa} ({code}): "
                             f"<code>{_format_price(usd_toman / rate)}</code> تومان")
                added += 1
        if not added:
            lines.append(f"{E('warn')} نرخ ارزهای دیگر از منبع جهانی دریافت نشد.")
    else:
        lines.append(f"{E('cross')} نرخ دلار از هیچ منبعی دریافت نشد.")

    lines.append(DIVIDER)
    if gold_usd_oz:
        lines.append(f"{E('gold_oz')} انس طلا: <code>${_format_price(gold_usd_oz, 2)}</code>")
        if usd_toman:
            gram_24k = (gold_usd_oz / 31.1034768) * usd_toman
            lines.append(f"{E('gold24')} طلای ۲۴ عیار محاسباتی: "
                         f"<code>{_format_price(gram_24k)}</code> تومان/گرم")
            lines.append(f"{E('gold18')} طلای ۱۸ عیار محاسباتی: "
                         f"<code>{_format_price(gram_24k * 18 / 24)}</code> تومان/گرم")
    else:
        lines.append(f"{E('warn')} قیمت انس طلا دریافت نشد.")

    lines.append(DIVIDER)
    if usd_source:
        lines.append(f"{E('info')} منبع دلار: <code>{esc(usd_source)}</code>")
    if usd_errors:
        lines.append(f"{E('warn')} منابع ناموفق: <code>{esc('، '.join(usd_errors))}</code>")
    lines.append(f"{E('info')} نرخ ارزهای خارجی از روی دلار محاسبه شده و طلای ۱۸/۲۴ عیار "
                 f"محاسباتی است؛ ممکن است با بازار خرده‌فروشی اختلاف داشته باشد.")
    lines.append(f"{E('clock')} به‌روزرسانی: "
                 f"<code>{_localnow().strftime('%H:%M:%S')}</code>")

    result = "\n".join(lines)
    if usd_toman:      # فقط نتیجه‌ی موفق کش می‌شود
        _price_cache.update({"time": now, "text": result})
    return result


async def prices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    force = bool(context.args and context.args[0].lower() in ("refresh", "force", "تازه"))
    status = await reply(update, f"{E('chart')} در حال دریافت قیمت‌های بازار ...")
    try:
        text = await _build_market_prices(force=force)
    except Exception as e:
        logger.exception(f"خطای غیرمنتظره در قیمت بازار: {e}")
        text = f"{E('cross')} دریافت قیمت‌ها ناموفق بود. چند لحظه بعد دوباره امتحان کن."
    if status is None:
        await reply(update, text)
        return
    try:
        await status.edit_text(text, parse_mode="HTML")
    except TelegramError:
        try:
            await status.edit_text(strip_html(text))
        except TelegramError:
            await reply(update, text)


# ---------------------------------------------------------------------------
# تبلیغات کانال — عکس + دکمه‌ی شیشه‌ای  [قابلیت جدید]
# ---------------------------------------------------------------------------
_ADV_BUTTON_LINE_RE = re.compile(r'^\s*(?:دکمه|button)\s*[:٫]\s*(.+)$', re.IGNORECASE)
_ADV_BUTTON_SEG_RE = re.compile(r'(.+?)\s*(?:>|→|\|)\s*(https?://\S+)')


def _command_payload(update: Update) -> str:
    """متن/کپشن پیام را با حذف اولین کلمه (نام دستور) و حفظ کامل خط‌های بعدی برمی‌گرداند."""
    msg = update.effective_message
    raw = (msg.text or msg.caption or "") if msg else ""
    parts = raw.split(None, 1)
    return parts[1] if len(parts) > 1 else ""


def parse_adv_payload(payload: str):
    """ورودی چندخطی را پارس می‌کند.
    خط اول = مقصد (آیدی/یوزرنیم کانال)، خط‌های بعدی = متن،
    خط‌هایی که با «دکمه:» شروع شوند = دکمه‌ی شیشه‌ای.
    -> (target_raw, body_text, keyboard_rows[list[list[InlineKeyboardButton]]])
    """
    lines = payload.split("\n")
    target, idx = None, 0
    for i, line in enumerate(lines):
        if line.strip():
            target = line.strip()
            idx = i + 1
            break
    if target is None:
        return None, "", []

    body_lines, keyboard_rows = [], []
    for line in lines[idx:]:
        m = _ADV_BUTTON_LINE_RE.match(line)
        if m:
            row = []
            for seg in m.group(1).split("&&"):
                sm = _ADV_BUTTON_SEG_RE.match(seg.strip())
                if sm:
                    row.append(InlineKeyboardButton(sm.group(1).strip(), url=sm.group(2).strip()))
            if row:
                keyboard_rows.append(row)
            continue
        body_lines.append(line)

    body = "\n".join(body_lines).strip("\n")
    return target, body, keyboard_rows


async def adv_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ساخت و ارسال پست تبلیغاتی (عکس + دکمه‌ی شیشه‌ای) به یک کانال/گروه.
    فقط برای مالک اصلی یا ادمین‌های ثبت‌شده‌ی بات (نه هر ادمین گروه) کار می‌کند."""
    user = update.effective_user
    if not (is_owner(user.id) or user.id in get_db_admin_ids()):
        await reply(update, f"{E('cross')} این دستور فقط برای مالک اصلی یا ادمین‌های ثبت‌شده‌ی بات است.")
        return

    msg = update.effective_message
    payload = _command_payload(update)
    if not payload.strip():
        await reply(
            update,
            f"{E('info')} فرمت درست (روی یک عکس به‌عنوان کپشن بفرست، یا روی عکس ریپلای کن):\n"
            f"<code>/adv @channel_یا_آیدی_عددی\n"
            f"متن تبلیغ ...\n\n"
            f"دکمه: 🛒 خرید &gt; https://example.com</code>\n"
            f"{E('info')} برای چند دکمه در یک ردیف از <code>&amp;&amp;</code> استفاده کن.",
        )
        return

    target_raw, body, keyboard_rows = parse_adv_payload(payload)
    if not target_raw:
        await reply(update, f"{E('cross')} خط اول باید آیدی/یوزرنیم کانال یا گروه مقصد باشد.")
        return

    try:
        target_chat_id = int(target_raw)
    except ValueError:
        target_chat_id = target_raw if target_raw.startswith("@") else f"@{target_raw}"

    photo_file_id = None
    if msg.photo:
        photo_file_id = msg.photo[-1].file_id
    elif msg.reply_to_message and msg.reply_to_message.photo:
        photo_file_id = msg.reply_to_message.photo[-1].file_id

    kb = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

    try:
        if photo_file_id:
            await context.bot.send_photo(
                chat_id=target_chat_id, photo=photo_file_id,
                caption=body or None, parse_mode="HTML", reply_markup=kb,
            )
        else:
            if not body:
                await reply(update, f"{E('cross')} یا عکس بفرست یا متن تبلیغ را بنویس.")
                return
            await context.bot.send_message(
                chat_id=target_chat_id, text=body, parse_mode="HTML", reply_markup=kb,
            )
    except TelegramError as e:
        await reply(
            update,
            f"{E('cross')} ارسال ناموفق بود: {esc(e)}\n"
            f"{E('info')} چک کن که بات در آن کانال/گروه <b>ادمین</b> است و "
            f"دسترسی ارسال پیام دارد.",
        )
        return

    await reply(update, f"{E('check')} تبلیغ با موفقیت به <code>{esc(target_raw)}</code> ارسال شد.")


# ---------------------------------------------------------------------------
# پنل دکمه‌ای
# ---------------------------------------------------------------------------
def group_list_keyboard():
    rows = core_groups_list()
    buttons = [
        [btn("target", title or "بدون‌نام", callback_data=f"m:grp:{cid}")]
        for cid, title in rows
    ]
    return rows, InlineKeyboardMarkup(buttons) if buttons else None


def range_menu_keyboard(chat_id: int):
    cur_mn, cur_mx = get_group_range(chat_id)
    rows = []
    for mn, mx in RANGE_PRESETS:
        label = f"{mn} تا {mx}"
        if (mn, mx) == (cur_mn, cur_mx):
            label = f"✅ {label}"
        rows.append([InlineKeyboardButton(label, callback_data=f"a:setrange:{chat_id}:{mn}:{mx}")])
    rows.append([btn("back", "بازگشت", callback_data=f"a:backmain:{chat_id}")])
    return InlineKeyboardMarkup(rows)


def mgmt_menu_keyboard(chat_id: int):
    """دکمه‌های روشن/خاموش برای تنظیمات مدیریت."""
    m = get_group_mgmt(chat_id)
    def tog(field, label):
        mark = E("on") if m[field] else E("off")
        return InlineKeyboardButton(f"{mark} {label}",
                                    callback_data=f"a:tog:{chat_id}:{field}")
    rows = [
        [tog("antispam", "ضد اسپم"), tog("antilink", "ضد لینک")],
        [tog("welcome", "خوش‌آمد"), tog("night_mode", "حالت شب")],
        [tog("antiforward", "ضد فوروارد"), tog("captcha", "کپچا اعضای جدید")],
        [btn("refresh", "تازه‌سازی", callback_data=f"a:mgmtsettings:{chat_id}")],
        [btn("back", "بازگشت", callback_data=f"a:backmain:{chat_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def action_keyboard(chat_id: int, admin: bool, show_back: bool):
    rows = []
    if admin:
        cur_mn, cur_mx = get_group_range(chat_id)
        rows.append([
            btn("party", "شروع", callback_data=f"a:start:{chat_id}"),
            btn("lock", "پایان ثبت‌نام", callback_data=f"a:stop:{chat_id}"),
        ])
        rows.append([
            btn("medal1", "۱ برنده", callback_data=f"a:pick:{chat_id}:1"),
            btn("medal2", "۳ برنده", callback_data=f"a:pick:{chat_id}:3"),
            btn("medal3", "۵ برنده", callback_data=f"a:pick:{chat_id}:5"),
        ])
        rows.append([btn("doc", "خروجی اکسل", callback_data=f"a:export:{chat_id}")])
        rows.append([btn("range", f"بازه‌ی عدد ({cur_mn}-{cur_mx})",
                         callback_data=f"a:rangemenu:{chat_id}")])
    rows.append([
        btn("chart", "وضعیت", callback_data=f"a:status:{chat_id}"),
        btn("scroll", "تاریخچه", callback_data=f"a:hist:{chat_id}"),
    ])
    rows.append([btn("chart", "قیمت بازار", callback_data=f"a:prices:{chat_id}")])
    rows.append([
        btn("id", "عدد من", callback_data=f"a:myentry:{chat_id}"),
        btn("leaderboard", "برترین‌ها", callback_data=f"a:leaderboard:{chat_id}"),
    ])
    rows.append([
        btn("level", "آمار من", callback_data=f"a:mystats:{chat_id}"),
        btn("fire", "فعال‌ترین‌ها", callback_data=f"a:topactive:{chat_id}"),
    ])
    if admin:
        rows.append([
            btn("gear", "تنظیمات گروه", callback_data=f"a:mgmtsettings:{chat_id}"),
            btn("shield", "تست ضد اسپم", callback_data=f"a:diag:{chat_id}"),
        ])
    if show_back:
        rows.append([btn("back", "بازگشت به لیست گروه‌ها", callback_data="m:groups")])
    return InlineKeyboardMarkup(rows)


async def render_menu(chat_type: str, chat_id: int, admin: bool):
    if chat_type == "private":
        rows, kb = group_list_keyboard()
        if not rows:
            return (
                f"{E('info')} هنوز هیچ گروهی شناسایی نشده.\n"
                f"بات را به گروه اضافه کن و یک پیام (حتی /help) آنجا بفرست.",
                None,
            )
        return f"{E('menu')} <b>پنل مدیریت</b>\nیک گروه را انتخاب کن:", kb
    return f"{E('menu')} <b>پنل مدیریت این گروه</b>", action_keyboard(chat_id, admin, False)


# ---------------------------------------------------------------------------
# دستورهای عمومی و قرعه‌کشی
# ---------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(
        update,
        f"{E('sparkle')} <b>به بات خوش آمدی!</b>\n\n"
        f"با این بات می‌توانی توی گروهت قرعه‌کشی عددی برگزار کنی، سطح و آمار اعضا را "
        f"ببینی و گروهت را مدیریت کنی.\n"
        f"پنل دکمه‌ای: /menu\n"
        f"لیست دستورها: /help",
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    admin = await is_admin(update, context)
    if chat.type == "private" and not admin:
        await reply(update, f"{E('cross')} پنل مدیریتی فقط برای ادمین است. "
                            f"توی گروه از /status و /myentry استفاده کن.")
        return
    text, kb = await render_menu(chat.type, chat.id, admin)
    await reply(update, text, reply_markup=kb)


async def start_lottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context,
                               f"{E('cross')} فقط ادمین می‌تواند قرعه‌کشی را شروع کند."):
        return
    chat_id, _, err = await resolve_target(update, context)
    if err:
        await reply(update, err)
        return
    ok, text = core_start_lottery(chat_id)
    if not ok:
        await reply(update, text)
        return
    await announce(context, chat_id, text)
    if update.effective_chat.type == "private":
        await reply(update, f"{E('check')} قرعه‌کشی توی گروه هدف شروع شد.")


async def stop_lottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context,
                               f"{E('cross')} فقط ادمین می‌تواند ثبت‌نام را ببندد."):
        return
    chat_id, _, err = await resolve_target(update, context)
    if err:
        await reply(update, err)
        return
    ok, text = core_stop_lottery(chat_id)
    if not ok:
        await reply(update, text)
        return
    await announce(context, chat_id, text)
    if update.effective_chat.type == "private":
        await reply(update, f"{E('check')} ثبت‌نام بسته شد. حالا /pickwinner بزن.")


async def cancel_lottery_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id, _, err = await resolve_target(update, context)
    if err:
        await reply(update, err)
        return
    ok, text = core_cancel_lottery(chat_id)
    await reply(update, text)
    if ok and update.effective_chat.type == "private":
        await announce(context, chat_id, text)


async def pick_winner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context,
                               f"{E('cross')} فقط ادمین می‌تواند برنده را مشخص کند."):
        return
    chat_id, extra_args, err = await resolve_target(update, context)
    if err:
        await reply(update, err)
        return
    winners_count = 3
    if extra_args:
        try:
            winners_count = max(1, min(50, int(extra_args[0])))
        except ValueError:
            await reply(update, f"{E('warn')} عدد نامعتبر. مثال: <code>/pickwinner 3</code>")
            return
    ok, text = core_pick_winner(chat_id, winners_count)
    if not ok:
        await reply(update, text)
        return
    try:
        await context.bot.send_dice(chat_id=chat_id, emoji="🎰")
        await asyncio.sleep(3)
    except TelegramError as e:
        logger.warning(f"ارسال انیمیشن اسلات ناموفق: {e}")
    await announce(context, chat_id, text)
    if update.effective_chat.type == "private":
        await reply(update, f"{E('check')} نتیجه توی گروه هدف اعلام شد.")


async def _chat_id_for_readonly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """برای دستورهای فقط-خواندنی: در گروه همان گروه، در پیوی نیاز به ادمین + آیدی."""
    chat = update.effective_chat
    if chat.type != "private":
        return chat.id, None
    if not await is_admin(update, context):
        return None, f"{E('cross')} این دستور توی پیوی فقط برای ادمین است."
    chat_id, _, err = await resolve_target(update, context)
    return chat_id, err


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, err = await _chat_id_for_readonly(update, context)
    if err:
        await reply(update, err)
        return
    await reply(update, core_status_text(chat_id))


async def my_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, core_my_entry_text(update.effective_chat.id, update.effective_user.id))


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context,
                               f"{E('cross')} فقط ادمین می‌تواند خروجی بگیرد."):
        return
    chat_id, _, err = await resolve_target(update, context)
    if err:
        await reply(update, err)
        return
    tmp_path, lottery_id = core_build_export_workbook(chat_id)
    if tmp_path == "NO_OPENPYXL":
        await reply(update, f"{E('warn')} کتابخانه openpyxl نصب نیست: "
                            f"<code>pip install openpyxl</code>")
        return
    if not tmp_path:
        await reply(update, f"{E('info')} هیچ شرکت‌کننده‌ای برای خروجی وجود ندارد.")
        return
    try:
        with open(tmp_path, "rb") as f:
            await update.effective_message.reply_document(
                document=InputFile(f, filename=f"lottery_{lottery_id}.xlsx"),
                caption=f"{E('doc')} خروجی قرعه‌کشی شماره {lottery_id}",
            )
    except TelegramError as e:
        await reply(update, f"{E('cross')} ارسال فایل ناموفق: {esc(e)}")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, err = await _chat_id_for_readonly(update, context)
    if err:
        await reply(update, err)
        return
    await reply(update, core_history_text(chat_id))


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, err = await _chat_id_for_readonly(update, context)
    if err:
        await reply(update, err)
        return
    await reply(update, core_leaderboard_text(chat_id))


async def setrange_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id, extra_args, err = await resolve_target(update, context)
    if err:
        await reply(update, err)
        return
    if len(extra_args) < 2:
        await reply(update,
                    f"{E('info')} فرمت درست: <code>/setrange حداقل حداکثر</code>\n"
                    f"مثال: <code>/setrange 1 500</code>\n"
                    f"یا از دکمه‌ی «بازه‌ی عدد» در /menu استفاده کن.")
        return
    try:
        mn = int(str(extra_args[0]).translate(_PERSIAN_DIGITS))
        mx = int(str(extra_args[1]).translate(_PERSIAN_DIGITS))
    except ValueError:
        await reply(update, f"{E('cross')} حداقل و حداکثر باید عدد صحیح باشند.")
        return
    if mn < 1 or mx <= mn or mx > RANGE_HARD_CAP:
        await reply(update,
                    f"{E('cross')} بازه نامعتبر است. حداقل ≥ ۱، حداکثر > حداقل، "
                    f"و سقف مجاز <code>{RANGE_HARD_CAP}</code>.")
        return
    set_group_range(chat_id, mn, mx)
    await reply(update,
                f"{E('check')} بازه‌ی عدد برای قرعه‌کشی‌های بعدی: "
                f"<code>{mn}</code> تا <code>{mx}</code>\n"
                f"{E('info')} روی قرعه‌کشی بازِ فعلی اثری ندارد.")


async def groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    rows = core_groups_list()
    if not rows:
        await reply(update, f"{E('info')} هنوز هیچ گروهی شناسایی نشده.\n"
                            f"بات را به گروه اضافه کن و یک پیام آنجا بفرست.")
        return
    lines = [f"{E('scroll')} <b>گروه‌هایی که بات می‌شناسد</b>", DIVIDER]
    for cid, title in rows:
        lines.append(f"{E('pin')} {esc(title or 'بدون‌نام')} — <code>{cid}</code>")
    lines.append(DIVIDER)
    lines.append(f"{E('info')} برای اجرای دستور از پیوی، آیدی را بعد از دستور بگذار.\n"
                 f"مثال: <code>/startlottery {rows[0][0]}</code>\n"
                 f"یا راحت‌تر: /menu")
    await reply(update, "\n".join(lines))


async def emojis_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    custom_ids = _load_custom_emoji_ids()
    lines = [f"{E('sparkle')} <b>لیست تگ‌های ایموجی</b>", DIVIDER]
    for key, info in EMOJI_DEFS.items():
        is_custom = bool(custom_ids.get(key) or info.get("custom_emoji_id"))
        state = "سفارشی" if is_custom else "پیش‌فرض"
        lines.append(f"{E(key)}  <code>{key}</code> — {esc(info['usage'])}  ({state})")
    lines.append(DIVIDER)
    mode = "خاموش" if DISABLE_CUSTOM_EMOJI else "روشن"
    lines.append(
        f"{E('info')} ایموجی سفارشی الان <b>{mode}</b> است "
        f"(<code>DISABLE_CUSTOM_EMOJI</code>).\n"
        f"{E('warn')} اگر بات پرمیوم نباشد و این گزینه روشن باشد، تلگرام پیام‌ها را رد می‌کند.\n"
        f"{E('info')} برای شخصی‌سازی، <code>emojis.json</code> را ویرایش کن."
    )
    # پیام طولانی است — تکه‌تکه بفرست
    await _reply_long(update, "\n".join(lines))


async def _reply_long(update: Update, text: str, limit: int = 3800):
    """متن‌های طولانی را تکه‌تکه می‌فرستد تا خطای «message is too long» ندهد."""
    lines = text.split("\n")
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            await reply(update, buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        await reply(update, buf)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    text = f"{E('id')} آیدی عددی شما: <code>{user.id}</code>"
    if chat.type in GROUP_TYPES:
        text += f"\n{E('id')} آیدی این گروه: <code>{chat.id}</code>"
    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        t = msg.reply_to_message.from_user
        text += f"\n{E('id')} آیدی {esc(t.full_name)}: <code>{t.id}</code>"
    text += (f"\n{E('info')} این آیدی را در متغیر محیطی <code>OWNER_IDS</code> بگذار "
             f"تا دسترسی مدیریتی داشته باشی.")
    await reply(update, text)


async def emojiid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """روی پیامی که ایموجی‌های پرمیوم واقعی توش هست ریپلای کن و /emojiid بزن."""
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    target = msg.reply_to_message if msg else None
    if not target:
        await reply(update,
                    f"{E('info')} روی پیامی که ایموجی پرمیوم واقعی دارد ریپلای کن و "
                    f"دوباره <code>/emojiid</code> بزن.")
        return
    entities = list(target.entities or []) + list(target.caption_entities or [])
    custom = [e for e in entities if getattr(e, "type", None) == "custom_emoji"]
    if not custom:
        await reply(update, f"{E('warn')} توی این پیام هیچ ایموجی سفارشی/پرمیومی پیدا نشد.")
        return
    src_text = target.text or target.caption or ""
    lines = [f"{E('sparkle')} <b>ایموجی‌های سفارشیِ این پیام</b>", DIVIDER]
    for e in custom:
        alt = src_text[e.offset: e.offset + e.length]
        lines.append(f"{alt} → id=<code>{esc(e.custom_emoji_id)}</code>, "
                     f"alt=<code>{esc(alt)}</code>")
    first = custom[0]
    first_alt = src_text[first.offset: first.offset + first.length]
    lines.append(DIVIDER)
    lines.append(f"{E('info')} این جفتِ id+alt را دقیقاً همین‌طور در "
                 f"<code>emojis.json</code> بگذار، مثلاً:\n"
                 f"<code>\"party\": {{\"id\": \"{esc(first.custom_emoji_id)}\", "
                 f"\"alt\": \"{esc(first_alt)}\"}}</code>")
    await reply(update, "\n".join(lines))


def _resolve_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        return u.id, u.username, u.full_name
    if context.args:
        arg = context.args[0].lstrip("@")
        try:
            return int(arg.translate(_PERSIAN_DIGITS)), None, None
        except ValueError:
            return None, None, None
    return None, None, None


async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await reply(update, f"{E('cross')} فقط مالک اصلی می‌تواند ادمین اضافه کند.")
        return
    target_id, username, full_name = _resolve_target_user(update, context)
    if target_id is None:
        await reply(update,
                    f"{E('info')} فرمت: <code>/addadmin آیدی_عددی</code>\n"
                    f"یا روی پیام کاربر ریپلای کن و بنویس <code>/addadmin</code>")
        return
    if is_owner(target_id):
        await reply(update, f"{E('info')} این کاربر همین حالا هم مالک اصلی است.")
        return
    add_admin_db(target_id, username, full_name, update.effective_user.id)
    label = f"@{esc(username)}" if username else f"<code>{target_id}</code>"
    await reply(update, f"{E('check')} {label} به لیست ادمین‌ها اضافه شد.")


async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await reply(update, f"{E('cross')} فقط مالک اصلی می‌تواند ادمین حذف کند.")
        return
    target_id, _, _ = _resolve_target_user(update, context)
    if target_id is None:
        await reply(update, f"{E('info')} فرمت: <code>/removeadmin آیدی_عددی</code> "
                            f"یا ریپلای روی پیام کاربر.")
        return
    if is_owner(target_id):
        await reply(update, f"{E('cross')} مالک اصلی را باید از <code>OWNER_IDS</code> حذف کنی.")
        return
    if remove_admin_db(target_id):
        await reply(update, f"{E('check')} دسترسی ادمینی <code>{target_id}</code> حذف شد.")
    else:
        await reply(update, f"{E('info')} این آیدی در لیست ادمین‌های اضافه‌شده نبود.")


async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    lines = [f"{E('sparkle')} <b>لیست دسترسی‌های مدیریتی</b>", DIVIDER,
             f"{E('trophy')} <b>مالک(های) اصلی</b> (از متغیر محیطی):"]
    if OWNER_IDS:
        for oid in sorted(OWNER_IDS):
            lines.append(f"{E('pin')} <code>{oid}</code>")
    else:
        lines.append(f"{E('warn')} هیچ مالکی تنظیم نشده!")
    lines.append(DIVIDER)
    db_admins = list_db_admins()
    if db_admins:
        lines.append(f"{E('id')} <b>ادمین‌های اضافه‌شده:</b>")
        for user_id, username, full_name, added_at in db_admins:
            label = f"@{esc(username)}" if username else esc(full_name or user_id)
            lines.append(f"{E('pin')} {label} — <code>{user_id}</code>")
    else:
        lines.append(f"{E('info')} هیچ ادمین اضافه‌ای ثبت نشده.")
    if is_owner(update.effective_user.id):
        lines.append(DIVIDER)
        lines.append(f"{E('info')} افزودن: <code>/addadmin آیدی</code> • "
                     f"حذف: <code>/removeadmin آیدی</code>")
    await reply(update, "\n".join(lines))


# ---------------------------------------------------------------------------
# دستورهای آمار و لول
# ---------------------------------------------------------------------------
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, err = await _chat_id_for_readonly(update, context)
    if err:
        await reply(update, err)
        return
    total_users, total_msgs, total_xp, max_level = get_chat_global_stats(chat_id)
    if not total_users:
        await reply(update, f"{E('info')} هنوز هیچ آماری برای این گروه ثبت نشده.")
        return
    await reply(update,
                f"{E('chart')} <b>آمار گروه</b>\n"
                f"{DIVIDER}\n"
                f"{E('id')} اعضای فعال: <code>{total_users}</code>\n"
                f"{E('star')} مجموع پیام‌ها: <code>{total_msgs}</code>\n"
                f"{E('level')} مجموع XP: <code>{total_xp}</code>\n"
                f"{E('crown')} بالاترین لول: <code>{max_level}</code>")


async def my_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await reply(update,
                    f"{E('info')} این دستور را توی گروه بزن تا آمارت را ببینی.\n"
                    f"یا از پنل گروه در /menu دکمه‌ی «آمار من» را بزن.")
        return
    await reply(update, render_profile_text(chat.id, update.effective_user,
                                            "آمار شما در این گروه"))


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await reply(update, f"{E('info')} این دستور را توی گروه بزن یا از دکمه‌ی "
                            f"«آمار من» در /menu استفاده کن.")
        return
    target_user = update.effective_user
    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        target_user = msg.reply_to_message.from_user
    title = f"پروفایل {target_user.full_name or target_user.id}"
    await reply(update, render_profile_text(chat.id, target_user, title))


async def top_active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, err = await _chat_id_for_readonly(update, context)
    if err:
        await reply(update, err)
        return
    top_xp = get_top_users(chat_id, limit=10, order_by="xp")
    if not top_xp:
        await reply(update, f"{E('info')} هنوز هیچ آماری برای این گروه ثبت نشده.")
        return
    lines = [f"{E('fire')} <b>فعال‌ترین اعضای گروه</b>", DIVIDER]
    for idx, (uid, uname, fname, mcount, xp, level) in enumerate(top_xp, start=1):
        lines.append(f"{medal_for(idx)} {mention_of(uname, fname)} — "
                     f"لول <code>{level}</code> • <code>{xp}</code> XP • "
                     f"<code>{mcount}</code> پیام")
    lines.append(DIVIDER)
    lines.append(f"{E('info')} برای آمار خودت: /mystats")
    await reply(update, "\n".join(lines))


# ---------------------------------------------------------------------------
# دستورهای مدیریت گروه
# ---------------------------------------------------------------------------
async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update):
        return
    rules = get_group_mgmt(update.effective_chat.id)["rules_text"]
    if not rules:
        await reply(update,
                    f"{E('rule')} <b>قوانین گروه</b>\n{DIVIDER}\n"
                    f"{E('info')} هنوز قانونی تنظیم نشده.\n"
                    f"ادمین می‌تواند با <code>/setrules متن</code> تنظیم کند.")
        return
    await reply(update, f"{E('rule')} <b>قوانین گروه</b>\n{DIVIDER}\n{esc(rules)}")


async def set_rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    text = " ".join(context.args) if context.args else ""
    if not text:
        await reply(update, f"{E('info')} فرمت: <code>/setrules متن قوانین گروه</code>")
        return
    update_group_mgmt(update.effective_chat.id, rules_text=text)
    await reply(update, f"{E('check')} قوانین گروه ثبت شد.")


async def set_welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    text = " ".join(context.args) if context.args else ""
    if not text:
        await reply(update,
                    f"{E('info')} فرمت: <code>/setwelcome متن خوش‌آمد</code>\n"
                    f"می‌توانی از <code>{{name}}</code>، <code>{{mention}}</code>، "
                    f"<code>{{group}}</code> و <code>{{count}}</code> استفاده کنی.")
        return
    update_group_mgmt(update.effective_chat.id, welcome_text=text, welcome=True)
    await reply(update, f"{E('check')} پیام خوش‌آمد تنظیم شد.")


async def _reply_target(update: Update):
    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user
    return None


class _FakeUser:
    """برای وقتی فقط آیدی عددی داریم."""
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.full_name = str(uid)
        self.is_bot = False


async def _target_or_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await _reply_target(update)
    if target:
        return target
    if context.args:
        try:
            return _FakeUser(int(str(context.args[0]).translate(_PERSIAN_DIGITS)))
        except ValueError:
            return None
    return None


async def _guard_target(update: Update, context: ContextTypes.DEFAULT_TYPE, target,
                        action: str) -> bool:
    """جلوی اعمال روی خود بات و ادمین‌ها را می‌گیرد."""
    chat_id = update.effective_chat.id
    if target.id == context.bot.id:
        await reply(update, f"{E('cross')} روی خودم {esc(action)} اعمال نمی‌کنم!")
        return False
    if await is_admin_of(context.bot, target.id, chat_id):
        await reply(update, f"{E('cross')} روی ادمین نمی‌شود {esc(action)} اعمال کرد.")
        return False
    return True


async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    target = await _reply_target(update)
    if not target:
        await reply(update, f"{E('info')} روی پیام کاربر ریپلای کن: <code>/warn دلیل</code>")
        return
    chat_id = update.effective_chat.id
    if not await _guard_target(update, context, target, "اخطار"):
        return

    reason = " ".join(context.args) if context.args else "بدون دلیل"
    count = add_warning(chat_id, target.id, reason)
    max_warn = get_group_mgmt(chat_id)["max_warnings"]

    if count >= max_warn:
        try:
            await context.bot.ban_chat_member(chat_id, target.id)
            reset_warnings(chat_id, target.id)
            await reply(update,
                        f"{E('ban')} {mention_of(target.username, target.full_name)} "
                        f"به دلیل {max_warn} اخطار بن شد.\nآخرین دلیل: {esc(reason)}")
            return
        except TelegramError as e:
            logger.warning(f"خطا در بن: {e}")
            await reply(update, f"{E('cross')} بن ناموفق بود (دسترسی ادمین بات را چک کن): "
                                f"{esc(e)}")
            return

    await reply(update,
                f"{E('warning')} {mention_of(target.username, target.full_name)} اخطار گرفت.\n"
                f"تعداد اخطار: <code>{count}/{max_warn}</code>\n"
                f"دلیل: {esc(reason)}")


async def unwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    target = await _target_or_id(update, context)
    if not target:
        await reply(update, f"{E('info')} روی پیام کاربر ریپلای کن یا آیدی عددی بده.")
        return
    count = remove_warning(update.effective_chat.id, target.id)
    await reply(update, f"{E('check')} یک اخطار از "
                        f"{mention_of(target.username, target.full_name)} کم شد. "
                        f"باقی‌مانده: <code>{count}</code>")


async def warnings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update):
        return
    target = await _target_or_id(update, context) or update.effective_user
    count = get_warnings(update.effective_chat.id, target.id)
    await reply(update, f"{E('warning')} {mention_of(target.username, target.full_name)} — "
                        f"<code>{count}</code> اخطار")


async def resetwarns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    target = await _target_or_id(update, context)
    if not target:
        await reply(update, f"{E('info')} روی پیام کاربر ریپلای کن یا آیدی عددی بده.")
        return
    reset_warnings(update.effective_chat.id, target.id)
    await reply(update, f"{E('check')} اخطارهای "
                        f"{mention_of(target.username, target.full_name)} پاک شد.")


UNMUTE_PERMISSIONS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True,
    can_invite_users=True,
)

MUTE_PERMISSIONS = ChatPermissions(
    can_send_messages=False, can_send_audios=False, can_send_documents=False,
    can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
    can_send_voice_notes=False, can_send_polls=False,
    can_send_other_messages=False, can_add_web_page_previews=False,
)


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    target = await _reply_target(update)
    dur_args = list(context.args or [])
    if not target:
        # بدون ریپلای: آرگومان اول باید آیدی عددی باشد و بقیه مدت زمان
        target = await _target_or_id(update, context)
        dur_args = dur_args[1:]
        if not target:
            await reply(update,
                        f"{E('info')} روی پیام کاربر ریپلای کن و بنویس "
                        f"«سکوت کن ۱۰ دقیقه» یا <code>/mute 10</code>\n"
                        f"بدون ریپلای: <code>/mute آیدی_عددی 10</code>")
            return
    chat_id = update.effective_chat.id
    if not await _guard_target(update, context, target, "سایلنت"):
        return
    minutes = parse_duration_minutes(dur_args, default=10)
    until = _utcnow() + timedelta(minutes=minutes)
    mute_user(chat_id, target.id, until, "سایلنت توسط ادمین")
    ok = True
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id, user_id=target.id,
            permissions=MUTE_PERMISSIONS, until_date=until,
        )
    except TelegramError as e:
        ok = False
        logger.warning(f"خطا در سایلنت: {e}")
    note = "" if ok else (f"\n{E('warn')} محدودسازی در تلگرام ناموفق بود "
                          f"(دسترسی «محدود کردن اعضا» را به بات بده) — "
                          f"فعلاً فقط پیام‌هایش حذف می‌شود.")
    await reply(update, f"{E('mute')} {mention_of(target.username, target.full_name)} "
                        f"برای <code>{human_duration(minutes)}</code> سایلنت شد.{note}")


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    target = await _target_or_id(update, context)
    if not target:
        await reply(update, f"{E('info')} روی پیام کاربر ریپلای کن یا آیدی عددی بده.")
        return
    chat_id = update.effective_chat.id
    unmute_user(chat_id, target.id)
    clear_message_log(chat_id, target.id)
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id, user_id=target.id, permissions=UNMUTE_PERMISSIONS,
        )
    except TelegramError as e:
        logger.warning(f"خطا در آن‌میوت: {e}")
    await reply(update, f"{E('check')} {mention_of(target.username, target.full_name)} "
                        f"آن‌میوت شد.")


async def muted_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لیست کاربران سایلنت‌شده‌ی این گروه."""
    if not await require_group(update) or not await require_admin(update, context):
        return
    rows = list_muted(update.effective_chat.id)
    now = _utcnow()
    active = []
    for uid, until, reason in rows:
        dt = _parse_dt(until)
        if dt and dt > now:
            mins = int((dt - now).total_seconds() // 60) + 1
            active.append((uid, mins, reason))
    if not active:
        await reply(update, f"{E('info')} کسی در این گروه سایلنت نیست.")
        return
    lines = [f"{E('mute')} <b>کاربران سایلنت‌شده</b>", DIVIDER]
    for uid, mins, reason in active:
        lines.append(f"{E('pin')} <code>{uid}</code> — {human_duration(mins)} مانده"
                     f"{' • ' + esc(reason) if reason else ''}")
    lines.append(DIVIDER)
    lines.append(f"{E('info')} آن‌میوت: <code>/unmute آیدی</code>")
    await reply(update, "\n".join(lines))


async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    target = await _target_or_id(update, context)
    if not target:
        await reply(update, f"{E('info')} روی پیام کاربر ریپلای کن یا آیدی عددی بده.")
        return
    chat_id = update.effective_chat.id
    if not await _guard_target(update, context, target, "اخراج"):
        return
    try:
        await context.bot.ban_chat_member(chat_id, target.id)
        await context.bot.unban_chat_member(chat_id, target.id, only_if_banned=True)
        await reply(update, f"{E('kick')} {mention_of(target.username, target.full_name)} "
                            f"اخراج شد.")
    except TelegramError as e:
        await reply(update, f"{E('cross')} خطا: {esc(e)}")


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    target = await _target_or_id(update, context)
    if not target:
        await reply(update, f"{E('info')} روی پیام کاربر ریپلای کن یا آیدی عددی بده.")
        return
    chat_id = update.effective_chat.id
    if not await _guard_target(update, context, target, "بن"):
        return
    try:
        await context.bot.ban_chat_member(chat_id, target.id)
        await reply(update, f"{E('ban')} {mention_of(target.username, target.full_name)} بن شد.")
    except TelegramError as e:
        await reply(update, f"{E('cross')} خطا: {esc(e)}")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    target = await _target_or_id(update, context)
    if not target:
        await reply(update, f"{E('info')} روی پیام کاربر ریپلای کن یا آیدی عددی بده.")
        return
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target.id,
                                            only_if_banned=True)
        await reply(update, f"{E('check')} {mention_of(target.username, target.full_name)} "
                            f"آنبن شد.")
    except TelegramError as e:
        await reply(update, f"{E('cross')} خطا: {esc(e)}")


# ---- لیست سفید ----
async def whitelist_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    target = await _target_or_id(update, context)
    if not target:
        await reply(update, f"{E('info')} روی پیام کاربر ریپلای کن یا آیدی عددی بده.")
        return
    add_whitelist(update.effective_chat.id, target.id, update.effective_user.id)
    await reply(update, f"{E('check')} {mention_of(target.username, target.full_name)} "
                        f"به لیست سفید اضافه شد (معاف از ضد اسپم و ضد لینک).")


async def whitelist_remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    target = await _target_or_id(update, context)
    if not target:
        await reply(update, f"{E('info')} روی پیام کاربر ریپلای کن یا آیدی عددی بده.")
        return
    if remove_whitelist(update.effective_chat.id, target.id):
        await reply(update, f"{E('check')} از لیست سفید حذف شد.")
    else:
        await reply(update, f"{E('info')} این کاربر در لیست سفید نبود.")


async def whitelist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    ids = get_whitelist(update.effective_chat.id)
    if not ids:
        await reply(update, f"{E('info')} لیست سفید خالی است.\n"
                            f"افزودن: ریپلای + <code>/whitelist_add</code>")
        return
    lines = [f"{E('shield')} <b>لیست سفید</b>", DIVIDER]
    lines += [f"{E('pin')} <code>{i}</code>" for i in sorted(ids)]
    await reply(update, "\n".join(lines))


# ---- ابزارهای پیام ----
async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف پیامی که رویش ریپلای شده."""
    if not await require_group(update) or not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await reply(update, f"{E('info')} روی پیامی که می‌خواهی حذف شود ریپلای کن.")
        return
    ok = await _safe_delete(msg.reply_to_message)
    await _safe_delete(msg)
    if not ok:
        await reply(update, f"{E('cross')} حذف نشد — دسترسی «حذف پیام» را به بات بده.")


async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حذف گروهی پیام‌ها از پیام ریپلای‌شده تا همین‌جا (حداکثر ۲۰۰)."""
    if not await require_group(update) or not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await reply(update, f"{E('info')} روی اولین پیامی که می‌خواهی پاک شود ریپلای کن "
                            f"و <code>/purge</code> بزن.")
        return
    start_id = msg.reply_to_message.message_id
    end_id = msg.message_id
    if end_id - start_id > 200:
        await reply(update, f"{E('warn')} حداکثر ۲۰۰ پیام در هر بار.")
        return
    deleted = 0
    for mid in range(start_id, end_id + 1):
        try:
            await context.bot.delete_message(update.effective_chat.id, mid)
            deleted += 1
        except TelegramError:
            pass
        await asyncio.sleep(0.05)
    info = await announce(context, update.effective_chat.id,
                          f"{E('trash')} <code>{deleted}</code> پیام پاک شد.")
    if info:
        await asyncio.sleep(5)
        await _safe_delete(info)


async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await reply(update, f"{E('info')} روی پیامی که می‌خواهی پین شود ریپلای کن.")
        return
    silent = bool(context.args and context.args[0].lower() in ("silent", "بی‌صدا", "بیصدا"))
    try:
        await context.bot.pin_chat_message(
            update.effective_chat.id, msg.reply_to_message.message_id,
            disable_notification=silent,
        )
        await reply(update, f"{E('pin2')} پیام پین شد.")
    except TelegramError as e:
        await reply(update, f"{E('cross')} پین نشد: {esc(e)}")


async def unpin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    msg = update.effective_message
    try:
        if msg.reply_to_message:
            await context.bot.unpin_chat_message(update.effective_chat.id,
                                                 msg.reply_to_message.message_id)
        else:
            await context.bot.unpin_all_chat_messages(update.effective_chat.id)
        await reply(update, f"{E('check')} پین برداشته شد.")
    except TelegramError as e:
        await reply(update, f"{E('cross')} خطا: {esc(e)}")


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اطلاعات کامل یک کاربر در این گروه."""
    if not await require_group(update):
        return
    target = await _target_or_id(update, context) or update.effective_user
    chat_id = update.effective_chat.id
    msg_count, xp, level, streak = get_user_stats(chat_id, target.id)
    warns = get_warnings(chat_id, target.id)
    until, reason = get_mute_until(chat_id, target.id)
    status = "-"
    try:
        member = await context.bot.get_chat_member(chat_id, target.id)
        status = {
            ChatMemberStatus.OWNER: "مالک گروه",
            ChatMemberStatus.ADMINISTRATOR: "ادمین",
            ChatMemberStatus.MEMBER: "عضو",
            ChatMemberStatus.RESTRICTED: "محدودشده",
            ChatMemberStatus.LEFT: "خارج‌شده",
            ChatMemberStatus.BANNED: "بن‌شده",
        }.get(member.status, str(member.status))
    except TelegramError:
        pass
    mute_line = ""
    if until and until > _utcnow():
        mins = int((until - _utcnow()).total_seconds() // 60) + 1
        mute_line = (f"\n{E('mute')} سایلنت: <code>{human_duration(mins)}</code> مانده"
                     f"{' • ' + esc(reason) if reason else ''}")
    await reply(update,
                f"{E('id')} <b>اطلاعات کاربر</b>\n{DIVIDER}\n"
                f"{E('id')} نام: {mention_link(target.id, getattr(target, 'full_name', target.id))}\n"
                f"{E('id')} آیدی: <code>{target.id}</code>\n"
                f"{E('gear')} وضعیت: {esc(status)}\n"
                f"{E('level')} لول <code>{level}</code> • <code>{xp}</code> XP\n"
                f"{E('chart')} پیام‌ها: <code>{msg_count}</code> • "
                f"استریک: <code>{streak}</code> روز\n"
                f"{E('warning')} اخطارها: <code>{warns}</code>"
                f"{mute_line}")


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """گرفتن نسخه‌ی پشتیبان از دیتابیس (فقط مالک، فقط در پیوی)."""
    if not is_owner(update.effective_user.id):
        await reply(update, f"{E('cross')} فقط مالک اصلی.")
        return
    if update.effective_chat.type != "private":
        await reply(update, f"{E('warn')} این دستور را در پیوی بزن.")
        return
    tmp = os.path.join(tempfile.gettempdir(),
                       f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
    try:
        with db() as conn:
            dest = sqlite3.connect(tmp)
            conn.backup(dest)          # بکاپ امن حتی وقتی بات در حال کار است
            dest.close()
        with open(tmp, "rb") as f:
            await update.effective_message.reply_document(
                document=InputFile(f, filename=os.path.basename(tmp)),
                caption=f"{E('doc')} نسخه‌ی پشتیبان دیتابیس",
            )
    except Exception as e:
        logger.warning(f"بکاپ ناموفق: {e}")
        # اگر backup() نبود، کپی ساده
        try:
            shutil.copy(DB_PATH, tmp)
            with open(tmp, "rb") as f:
                await update.effective_message.reply_document(
                    document=InputFile(f, filename=os.path.basename(tmp)))
        except Exception as e2:
            await reply(update, f"{E('cross')} بکاپ ناموفق: {esc(e2)}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def mgmt_settings_text(chat_id: int) -> str:
    mgmt = get_group_mgmt(chat_id)
    on, off = E("on"), E("off")
    wl = len(get_whitelist(chat_id))
    return (
        f"{E('gear')} <b>تنظیمات مدیریت گروه</b>\n"
        f"{DIVIDER}\n"
        f"{E('shield')} ضد اسپم: {on if mgmt['antispam'] else off}\n"
        f"{E('link')} ضد لینک: {on if mgmt['antilink'] else off}\n"
        f"{E('trash')} ضد فوروارد: {on if mgmt['antiforward'] else off}\n"
        f"{E('hello')} خوش‌آمدگویی: {on if mgmt['welcome'] else off}\n"
        f"{E('night')} حالت شب: {on if mgmt['night_mode'] else off} "
        f"(از ساعت {mgmt['night_start']} تا {mgmt['night_end']} — "
        f"منطقه‌ی زمانی {esc(TZ_NAME)})\n"
        f"{E('warning')} حداکثر اخطار: <code>{mgmt['max_warnings']}</code>\n"
        f"{E('fire')} حد فلاد: <code>{mgmt['flood_limit']}</code> پیام در "
        f"<code>{mgmt['flood_window']}</code> ثانیه → "
        f"{'سایلنت' if mgmt['flood_action'] == 'mute' else 'اخطار'}\n"
        f"{E('id')} لیست سفید: <code>{wl}</code> نفر\n"
        f"{E('shield')} کپچا اعضای جدید: {on if mgmt['captcha'] else off}"
        + (f" — <code>{mgmt['captcha_minutes']}</code> دقیقه مهلت، در صورت شکست: "
           f"{'اخراج' if mgmt['captcha_action'] == 'kick' else 'بن'}" if mgmt['captcha'] else "")
        + "\n"
        f"{DIVIDER}\n"
        f"{E('info')} تغییر با دکمه‌های زیر یا دستور:\n"
        f"<code>/toggle antispam|antilink|welcome|night|antiforward|captcha</code>\n"
        f"<code>/setmaxwarn عدد</code> • <code>/setflood تعداد ثانیه</code>\n"
        f"<code>/setnight شروع پایان</code> • <code>/floodaction mute|warn</code>\n"
        f"<code>/setcaptcha دقیقه kick|ban</code>"
    )


async def mgmt_settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    await reply(update, mgmt_settings_text(chat_id),
                reply_markup=mgmt_menu_keyboard(chat_id))


TOGGLE_MAP = {"antispam": "antispam", "antilink": "antilink",
              "welcome": "welcome", "night": "night_mode",
              "night_mode": "night_mode", "antiforward": "antiforward",
              "captcha": "captcha"}


async def toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    if not context.args:
        await reply(update, f"{E('info')} فرمت: "
                            f"<code>/toggle antispam|antilink|welcome|night|antiforward|captcha</code>")
        return
    key = context.args[0].lower()
    if key not in TOGGLE_MAP:
        await reply(update, f"{E('cross')} گزینه نامعتبر. یکی از: "
                            f"antispam, antilink, welcome, night, antiforward, captcha")
        return
    field = TOGGLE_MAP[key]
    mgmt = get_group_mgmt(update.effective_chat.id)
    new_val = not mgmt[field]
    update_group_mgmt(update.effective_chat.id, **{field: new_val})
    log_action(update.effective_chat.id, update.effective_user.id,
              update.effective_user.full_name, "toggle_setting",
              detail=f"{key} → {'on' if new_val else 'off'}")
    state = f"{E('on')} فعال" if new_val else f"{E('off')} غیرفعال"
    await reply(update, f"{E('check')} {esc(key)} → {state}")


async def setcaptcha_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    if not context.args:
        await reply(update,
                    f"{E('info')} فرمت: <code>/setcaptcha دقیقه kick|ban</code>\n"
                    f"مثال: <code>/setcaptcha 3 kick</code>\n"
                    f"kick = اخراج (می‌تواند دوباره بپیوندد)، ban = بن کامل.\n"
                    f"{E('info')} برای روشن/خاموش کردن کپچا: <code>/toggle captcha</code>")
        return
    try:
        minutes = max(1, min(60, int(str(context.args[0]).translate(_PERSIAN_DIGITS))))
    except ValueError:
        await reply(update, f"{E('cross')} عدد دقیقه نامعتبر است.")
        return
    action = "kick"
    if len(context.args) > 1:
        a = context.args[1].lower()
        if a in ("ban", "بن"):
            action = "ban"
        elif a in ("kick", "اخراج"):
            action = "kick"
        else:
            await reply(update, f"{E('cross')} نوع اقدام باید kick یا ban باشد.")
            return
    update_group_mgmt(update.effective_chat.id, captcha_minutes=minutes,
                      captcha_action=action)
    log_action(update.effective_chat.id, update.effective_user.id,
              update.effective_user.full_name, "toggle_setting",
              detail=f"captcha → {minutes} دقیقه / {action}")
    await reply(update,
                f"{E('check')} تنظیمات کپچا: <code>{minutes}</code> دقیقه مهلت، "
                f"در صورت شکست <b>{'اخراج' if action == 'kick' else 'بن'}</b> می‌شود.\n"
                f"{E('info')} اگر کپچا خاموش است، با <code>/toggle captcha</code> روشنش کن.")


async def floodaction_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("mute", "warn", "سایلنت", "اخطار"):
        await reply(update, f"{E('info')} فرمت: <code>/floodaction mute</code> یا "
                            f"<code>/floodaction warn</code>\n"
                            f"mute = سایلنت خودکار، warn = فقط اخطار و حذف پیام.")
        return
    value = "mute" if arg in ("mute", "سایلنت") else "warn"
    update_group_mgmt(update.effective_chat.id, flood_action=value)
    await reply(update, f"{E('check')} واکنش به فلاد → "
                        f"<b>{'سایلنت' if value == 'mute' else 'اخطار'}</b>")


async def setmaxwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    if not context.args:
        await reply(update, f"{E('info')} فرمت: <code>/setmaxwarn عدد</code>")
        return
    try:
        n = max(1, min(100, int(str(context.args[0]).translate(_PERSIAN_DIGITS))))
    except ValueError:
        await reply(update, f"{E('cross')} عدد نامعتبر.")
        return
    update_group_mgmt(update.effective_chat.id, max_warnings=n)
    await reply(update, f"{E('check')} حداکثر اخطار → <code>{n}</code>")


async def setflood_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    if len(context.args or []) < 2:
        await reply(update, f"{E('info')} فرمت: <code>/setflood تعداد ثانیه</code>\n"
                            f"مثال: <code>/setflood 5 10</code> یعنی ۵ پیام در ۱۰ ثانیه.")
        return
    try:
        limit = max(2, min(100, int(str(context.args[0]).translate(_PERSIAN_DIGITS))))
        window = max(1, min(600, int(str(context.args[1]).translate(_PERSIAN_DIGITS))))
    except ValueError:
        await reply(update, f"{E('cross')} اعداد نامعتبر.")
        return
    update_group_mgmt(update.effective_chat.id, flood_limit=limit, flood_window=window)
    await reply(update, f"{E('check')} حد فلاد: <code>{limit}</code> پیام در "
                        f"<code>{window}</code> ثانیه")


async def setnight_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update) or not await require_admin(update, context):
        return
    if len(context.args or []) < 2:
        await reply(update, f"{E('info')} فرمت: <code>/setnight ساعت_شروع ساعت_پایان</code>\n"
                            f"مثال: <code>/setnight 0 7</code>")
        return
    try:
        s = int(str(context.args[0]).translate(_PERSIAN_DIGITS)) % 24
        e = int(str(context.args[1]).translate(_PERSIAN_DIGITS)) % 24
    except ValueError:
        await reply(update, f"{E('cross')} ساعت‌ها باید عدد باشند (۰ تا ۲۳).")
        return
    update_group_mgmt(update.effective_chat.id, night_start=s, night_end=e)
    await reply(update, f"{E('check')} حالت شب از ساعت <code>{s}</code> تا "
                        f"<code>{e}</code> (منطقه‌ی زمانی {esc(TZ_NAME)}) تنظیم شد.\n"
                        f"برای فعال‌سازی: <code>/toggle night</code>")


async def _set_mgmt_flag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             field: str, value: bool, label: str):
    if not await require_group(update) or not await require_admin(update, context):
        return
    update_group_mgmt(update.effective_chat.id, **{field: value})
    state = f"{E('on')} روشن" if value else f"{E('off')} خاموش"
    await reply(update, f"{E('shield')} {label} {state} شد.")


async def antispam_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_mgmt_flag_cmd(update, context, "antispam", True, "ضد اسپم")


async def antispam_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_mgmt_flag_cmd(update, context, "antispam", False, "ضد اسپم")


async def antilink_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_mgmt_flag_cmd(update, context, "antilink", True, "ضد لینک")


async def antilink_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_mgmt_flag_cmd(update, context, "antilink", False, "ضد لینک")


async def build_antispam_report(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    """گزارش کامل + تشخیص عیب ضد اسپم."""
    mgmt = get_group_mgmt(chat_id)
    rights = await bot_rights(context, chat_id)
    on, off = E("on"), E("off")

    problems = []
    if not mgmt["antispam"]:
        problems.append("ضد اسپم خاموش است → <code>/antispamon</code>")
    if not rights["admin"]:
        problems.append("بات ادمین گروه نیست.")
    else:
        if not rights["delete"]:
            problems.append("دسترسی «حذف پیام» به بات داده نشده.")
        if not rights["restrict"]:
            problems.append("دسترسی «محدود کردن اعضا» به بات داده نشده.")

    lines = [
        f"{E('shield')} <b>وضعیت ضد اسپم</b>",
        DIVIDER,
        f"{E('shield')} ضد اسپم: {on if mgmt['antispam'] else off}",
        f"{E('fire')} حد: <code>{mgmt['flood_limit']}</code> پیام در "
        f"<code>{mgmt['flood_window']}</code> ثانیه",
        f"{E('mute')} واکنش: "
        f"{'سایلنت ' + str(FLOOD_MUTE_MINUTES) + ' دقیقه‌ای' if mgmt['flood_action'] == 'mute' else 'اخطار خودکار'}",
        f"{E('link')} ضد لینک: {on if mgmt['antilink'] else off}",
        DIVIDER,
        f"{E('robot')} <b>دسترسی‌های بات</b>",
        f"{E('gear')} ادمین: {on if rights['admin'] else off}",
        f"{E('trash')} حذف پیام: {on if rights['delete'] else off}",
        f"{E('mute')} محدود کردن اعضا: {on if rights['restrict'] else off}",
        f"{E('pin2')} پین پیام: {on if rights['pin'] else off}",
    ]
    if rights["error"]:
        lines.append(f"{E('warn')} خطا در خواندن وضعیت: <code>{esc(rights['error'])}</code>")

    lines.append(DIVIDER)
    if problems:
        lines.append(f"{E('cross')} <b>مشکلات پیدا شده:</b>")
        for p in problems:
            lines.append(f"{E('pin')} {p}")
    else:
        lines.append(f"{E('check')} همه‌چیز درست است. ادمین‌ها و لیست سفید معاف‌اند، "
                     f"پس برای تست با یک اکانت عادی پیام بفرست.")

    lines.append(DIVIDER)
    lines.append(
        f"{E('warn')} اگر بات اصلاً پیام‌های عادی را نمی‌بیند، در BotFather "
        f"<code>/setprivacy</code> را روی <b>Disable</b> بگذار، بعد بات را از گروه "
        f"خارج و دوباره اضافه کن."
    )
    lines.append(f"{E('info')} تغییر حد: <code>/setflood تعداد ثانیه</code> • "
                 f"معاف کردن کاربر: ریپلای + <code>/whitelist_add</code>")
    return "\n".join(lines)


async def antispam_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group(update):
        return
    await reply(update, await build_antispam_report(context, update.effective_chat.id))


async def auditlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """در گروه: لاگ همان گروه (ادمین). در پیوی: نمای سراسری همه‌ی گروه‌ها (فقط مالک).
    با ریپلای روی پیام کسی، فقط اقدامات مربوط به همان فرد را نشان می‌دهد."""
    chat = update.effective_chat
    target = await _reply_target(update)

    if chat.type in GROUP_TYPES:
        if not await require_admin(update, context):
            return
        rows = get_audit_log(chat_id=chat.id, target_id=target.id if target else None, limit=25)
        title = (f"لاگ اقدامات — {esc(target.full_name)}" if target
                 else "لاگ اقدامات ادمین‌های این گروه")
        await _reply_long(update, render_audit_log(rows, title))
        return

    # پیوی
    if not is_owner(update.effective_user.id):
        await reply(update, f"{E('cross')} این نمای سراسری فقط برای مالک اصلی است.\n"
                            f"{E('info')} برای لاگ یک گروه خاص، همین دستور را داخل خودِ "
                            f"آن گروه بزن.")
        return
    rows = get_audit_log(chat_id=None, target_id=target.id if target else None, limit=30)
    await _reply_long(update, render_audit_log(rows, "لاگ سراسری همه‌ی گروه‌ها",
                                               show_chat=True))


# ---------------------------------------------------------------------------
# هندلر پیام‌های گروه (مدیریت + آمار)
# ---------------------------------------------------------------------------
LINK_REGEX = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|telegram\.dog/|@[A-Za-z]\w{3,}"
    r"|[A-Za-z0-9-]+\.(com|net|org|ir|info|io|me|xyz|site|online|shop|co)\b)",
    re.IGNORECASE,
)
ACTION_COOLDOWN: dict = {}       # (chat_id, user_id, kind) -> timestamp
_last_xp_at: dict = {}           # (chat_id, user_id) -> timestamp


def _cooldown_ok(key, seconds: int) -> bool:
    now = time.time()
    if now - ACTION_COOLDOWN.get(key, 0) < seconds:
        return False
    ACTION_COOLDOWN[key] = now
    return True


async def _safe_delete(message):
    try:
        await message.delete()
        return True
    except TelegramError as e:
        logger.debug(f"حذف پیام ناموفق: {e}")
        return False


def _in_night_window(start: int, end: int) -> bool:
    hour = _localnow().hour
    if start == end:
        return False
    return (start <= hour < end) if start < end else (hour >= start or hour < end)


def _has_link(msg) -> bool:
    """هم متن/کپشن و هم entityها را چک می‌کند (لینک مخفی در متن هم گرفته می‌شود)."""
    text_content = msg.text or msg.caption or ""
    if LINK_REGEX.search(text_content):
        return True
    for ent in list(msg.entities or []) + list(msg.caption_entities or []):
        if getattr(ent, "type", None) in ("url", "text_link", "mention"):
            return True
    return False


def _is_forward(msg) -> bool:
    if getattr(msg, "forward_origin", None):
        return True
    return bool(getattr(msg, "forward_date", None))


async def moderation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت گروه — قبل از همه‌ی هندلرهای دیگر اجرا می‌شود و همه‌ی انواع پیام
    (متن، استیکر، ویس، عکس، گیف، فایل…) را می‌بیند."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or chat.type not in GROUP_TYPES or user.is_bot:
        return
    # پیام‌های سیستمی (عضو جدید/خروج) را دست نزن
    if msg.new_chat_members or msg.left_chat_member:
        return

    # ---- کاربر سایلنت‌شده ----
    if is_muted(chat.id, user.id):
        await _safe_delete(msg)
        raise ApplicationHandlerStop

    # ---- هشدار اگر فرد در لیست سیاه اسکمرها باشد ----
    await maybe_warn_scammer(context, chat.id, user)

    # ادمین‌ها و لیست سفید معاف‌اند
    if user.id in await get_chat_admin_ids(context.bot, chat.id):
        return
    if user.id in get_whitelist(chat.id):
        return

    mgmt = get_group_mgmt(chat.id)

    # ---- حالت شب ----
    if mgmt["night_mode"] and _in_night_window(mgmt["night_start"], mgmt["night_end"]):
        await _safe_delete(msg)
        if _cooldown_ok((chat.id, user.id, "night"), 60):
            await announce(context, chat.id,
                           f"{E('night')} حالت شب فعال است (تا ساعت "
                           f"{mgmt['night_end']}). لطفاً سکوت کن.")
        raise ApplicationHandlerStop

    # ---- ضد فوروارد ----
    if mgmt["antiforward"] and _is_forward(msg):
        await _safe_delete(msg)
        if _cooldown_ok((chat.id, user.id, "fwd"), 20):
            await announce(context, chat.id,
                           f"{E('trash')} {mention_of(user.username, user.full_name)} "
                           f"فوروارد در این گروه مجاز نیست.")
        raise ApplicationHandlerStop

    # ---- ضد لینک ----
    if mgmt["antilink"] and _has_link(msg):
        await _safe_delete(msg)
        if _cooldown_ok((chat.id, user.id, "link"), 20):
            await announce(context, chat.id,
                           f"{E('link')} {mention_of(user.username, user.full_name)} "
                           f"ارسال لینک مجاز نیست.")
        raise ApplicationHandlerStop

    # ---- ضد اسپم (فلاد) ----
    if not mgmt["antispam"]:
        return

    count = log_and_count(chat.id, user.id, mgmt["flood_window"])
    if count < mgmt["flood_limit"]:
        return

    await _safe_delete(msg)                 # پیام اسپم همیشه حذف می‌شود
    if not _cooldown_ok((chat.id, user.id, "flood"), 30):
        raise ApplicationHandlerStop

    clear_message_log(chat.id, user.id)

    if mgmt["flood_action"] == "warn":
        warns = add_warning(chat.id, user.id, "اسپم")
        max_warn = mgmt["max_warnings"]
        if warns >= max_warn:
            try:
                await context.bot.ban_chat_member(chat.id, user.id)
                reset_warnings(chat.id, user.id)
                await announce(context, chat.id,
                               f"{E('ban')} {mention_of(user.username, user.full_name)} "
                               f"به دلیل {max_warn} اخطار اسپم بن شد.")
            except TelegramError as e:
                logger.warning(f"بن خودکار ناموفق: {e}")
        else:
            await announce(context, chat.id,
                           f"{E('warning')} {mention_of(user.username, user.full_name)} "
                           f"به دلیل اسپم اخطار گرفت "
                           f"(<code>{warns}/{max_warn}</code>).")
        raise ApplicationHandlerStop

    # واکنش پیش‌فرض: سایلنت
    until = _utcnow() + timedelta(minutes=FLOOD_MUTE_MINUTES)
    mute_user(chat.id, user.id, until, "فلاد")
    restricted = True
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id, user_id=user.id,
            permissions=MUTE_PERMISSIONS, until_date=until,
        )
    except TelegramError as e:
        restricted = False
        logger.warning(f"خطا در سایلنت فلاد: {e}")

    note = "" if restricted else (
        f"\n{E('warn')} بات دسترسی «محدود کردن اعضا» ندارد؛ فعلاً فقط "
        f"پیام‌هایش حذف می‌شود. با /antispam وضعیت را ببین."
    )
    await announce(context, chat.id,
                   f"{E('mute')} {mention_of(user.username, user.full_name)} "
                   f"به دلیل اسپم برای {FLOOD_MUTE_MINUTES} دقیقه سایلنت شد.{note}")
    raise ApplicationHandlerStop


async def xp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فقط آمار و XP — منطق مدیریت جدا شده و قبلاً اجرا شده است."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or chat.type not in GROUP_TYPES or user.is_bot:
        return
    if msg.new_chat_members or msg.left_chat_member:
        return

    if XP_COOLDOWN > 0:
        key = (chat.id, user.id)
        now = time.time()
        if now - _last_xp_at.get(key, 0) < XP_COOLDOWN:
            upsert_user_stats(chat.id, user.id, user.username, user.full_name,
                              msg_delta=1, xp_delta=0)
            return
        _last_xp_at[key] = now

    xp_delta = random.randint(min(XP_MIN, XP_MAX), max(XP_MIN, XP_MAX))
    leveled_up, new_level, _ = upsert_user_stats(
        chat.id, user.id, user.username, user.full_name,
        msg_delta=1, xp_delta=xp_delta,
    )
    if leveled_up:
        await announce(context, chat.id,
                       f"{E('up')} {mention_of(user.username, user.full_name)} "
                       f"به لول <code>{new_level}</code> رسید! {E('sparkle')}")


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return
    chat = update.effective_chat
    invalidate_admin_cache(chat.id)
    track_known_chat(chat)

    # اگر خود بات اضافه شده، راهنمای راه‌اندازی بفرست
    if any(m.id == context.bot.id for m in msg.new_chat_members):
        rights = await bot_rights(context, chat.id)
        hint = "" if rights["admin"] else (
            f"\n{E('warn')} برای کار کردن ضد اسپم، کپچا و مدیریت، بات را ادمین کن با "
            f"دسترسی «حذف پیام» و «محدود کردن اعضا»."
        )
        await announce(context, chat.id,
                       f"{E('sparkle')} سلام! من اضافه شدم.\n"
                       f"{E('menu')} پنل: /menu • راهنما: /help{hint}")
        return

    mgmt = get_group_mgmt(chat.id)
    member_count = None
    try:
        member_count = await context.bot.get_chat_member_count(chat.id)
    except TelegramError:
        pass

    whitelisted = get_whitelist(chat.id)

    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        upsert_user_stats(chat.id, member.id, member.username, member.full_name)

        # ---- کپچا ----
        if mgmt["captcha"] and member.id not in whitelisted:
            sent_ok = await send_captcha_challenge(context, chat, member, mgmt)
            if sent_ok:
                continue    # پیام خوش‌آمد بعد از قبولی کپچا فرستاده می‌شود

        # ---- خوش‌آمدگویی معمولی (کپچا خاموش، یا کاربر معاف، یا ارسال کپچا ناموفق) ----
        if not mgmt["welcome"]:
            continue
        await send_welcome_text(context, chat, member, mgmt, member_count)


async def send_welcome_text(context: ContextTypes.DEFAULT_TYPE, chat, member,
                            mgmt: dict, member_count=None):
    mention = mention_link(member.id, member.full_name)
    if mgmt["welcome_text"]:
        text = (mgmt["welcome_text"]
                .replace("{name}", esc(member.full_name))
                .replace("{mention}", mention)
                .replace("{group}", esc(chat.title or ""))
                .replace("{count}", str(member_count or "")))
    else:
        text = (f"{E('hello')} {mention} به گروه خوش آمدی!\n"
                f"{E('rule')} قوانین: /rules\n"
                f"{E('info')} با چت کردن XP و لول می‌گیری!")
        if member_count:
            text += f"\n{E('id')} نفر <code>{member_count}</code>ام گروه هستی."
    await announce(context, chat.id, text)


async def send_captcha_challenge(context: ContextTypes.DEFAULT_TYPE, chat, member,
                                 mgmt: dict) -> bool:
    """کاربر را محدود می‌کند و پیام کپچا می‌فرستد. True اگر موفق بود."""
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id, user_id=member.id, permissions=MUTE_PERMISSIONS,
        )
    except TelegramError as e:
        logger.warning(f"محدودسازی برای کپچا ناموفق ({chat.id}/{member.id}): {e}")
        await announce(context, chat.id,
                       f"{E('warn')} کپچا برای {mention_link(member.id, member.full_name)} "
                       f"فعال نشد چون بات دسترسی «محدود کردن اعضا» ندارد. با /antispam "
                       f"دسترسی‌ها را چک کن.")
        return False

    minutes = mgmt["captcha_minutes"]
    mention = mention_link(member.id, member.full_name)
    placeholder_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("در حال آماده‌سازی…", callback_data="noop")]]
    )
    sent = await announce(
        context, chat.id,
        f"{E('captcha')} {mention} خوش اومدی!\n"
        f"{E('info')} برای اینکه مطمئن شویم ربات نیستی، روی ایموجیِ درست بزن.\n"
        f"{E('timer')} مهلت: <code>{minutes}</code> دقیقه — وگرنه "
        f"{'اخراج' if mgmt['captcha_action'] == 'kick' else 'بن'} می‌شوی.",
        reply_markup=placeholder_kb,
    )
    if sent is None:
        try:
            await context.bot.restrict_chat_member(
                chat.id, member.id, permissions=UNMUTE_PERMISSIONS)
        except TelegramError:
            pass
        return False

    correct = create_captcha(chat.id, member.id, sent.message_id, minutes)
    try:
        await sent.edit_reply_markup(reply_markup=captcha_keyboard(chat.id, member.id, correct))
    except TelegramError as e:
        logger.warning(f"ست‌کردن دکمه‌های کپچا ناموفق: {e}")
    return True


async def captcha_button(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list):
    """پردازش کلیک روی دکمه‌های کپچا: cap:<user_id>:<emoji>"""
    query = update.callback_query
    chat_id = query.message.chat.id
    clicker_id = query.from_user.id

    try:
        owner_id = int(parts[1])
    except (IndexError, ValueError):
        return
    clicked_emoji = parts[2] if len(parts) > 2 else ""

    if clicker_id != owner_id:
        await query.answer("این کپچا برای شما نیست 🙂", show_alert=True)
        return

    row = get_captcha(chat_id, owner_id)
    if not row:
        await query.answer("این کپچا منقضی یا قبلاً پاسخ داده شده.", show_alert=True)
        return
    correct, message_id, _attempts, expires_at = row

    if clicked_emoji == correct:
        delete_captcha(chat_id, owner_id)
        try:
            await context.bot.restrict_chat_member(
                chat_id, owner_id, permissions=UNMUTE_PERMISSIONS)
        except TelegramError as e:
            logger.warning(f"رفع محدودیت بعد از کپچا ناموفق: {e}")
        await query.answer("تایید شد ✅ خوش اومدی!")
        member = query.from_user
        mention = mention_link(owner_id, member.full_name)
        await safe_edit(query,
                        f"{E('check')} {mention} با موفقیت کپچا را حل کرد. خوش اومدی!")
        mgmt = get_group_mgmt(chat_id)
        if mgmt["welcome"]:
            member_count = None
            try:
                member_count = await context.bot.get_chat_member_count(chat_id)
            except TelegramError:
                pass
            await send_welcome_text(context, query.message.chat, member, mgmt, member_count)
        return

    # پاسخ اشتباه
    new_attempts = bump_captcha_attempts(chat_id, owner_id)
    if new_attempts >= CAPTCHA_MAX_ATTEMPTS:
        mgmt = get_group_mgmt(chat_id)
        delete_captcha(chat_id, owner_id)
        action = mgmt["captcha_action"]
        try:
            if action == "ban":
                await context.bot.ban_chat_member(chat_id, owner_id)
            else:
                await context.bot.ban_chat_member(chat_id, owner_id)
                await context.bot.unban_chat_member(chat_id, owner_id, only_if_banned=True)
        except TelegramError as e:
            logger.warning(f"اقدام روی شکست کپچا ناموفق: {e}")
        log_action(chat_id, SYSTEM_ACTOR_ID, None, "captcha_fail",
                  target_id=owner_id, target_name=query.from_user.full_name,
                  detail=f"{new_attempts} تلاش اشتباه → "
                         f"{'بن' if action == 'ban' else 'اخراج'}")
        await query.answer("اشتباه بود ❌", show_alert=True)
        await safe_edit(query,
                        f"{E('cross')} کپچای {mention_link(owner_id, query.from_user.full_name)} "
                        f"ناموفق بود و "
                        f"{'بن' if action == 'ban' else 'از گروه اخراج'} شد.")
        return

    # تلاش دوباره با مجموعه‌ی جدید ایموجی
    new_correct = random.choice(CAPTCHA_EMOJI_POOL)
    with db(commit=True) as conn:
        conn.execute("UPDATE captcha_pending SET correct_emoji=? WHERE chat_id=? AND user_id=?",
                    (new_correct, chat_id, owner_id))
    await query.answer(f"اشتباه بود، {CAPTCHA_MAX_ATTEMPTS - new_attempts} تلاش دیگر داری ❌",
                       show_alert=True)
    try:
        await query.edit_message_reply_markup(
            reply_markup=captcha_keyboard(chat_id, owner_id, new_correct))
    except TelegramError:
        pass


async def captcha_expiry_job(context: ContextTypes.DEFAULT_TYPE):
    """هر دقیقه اجرا می‌شود: کاربرهایی که به کپچا پاسخ نداده‌اند را اخراج/بن می‌کند."""
    for chat_id, user_id, message_id in list_expired_captchas():
        mgmt = get_group_mgmt(chat_id)
        action = mgmt.get("captcha_action", "kick")
        delete_captcha(chat_id, user_id)
        try:
            if action == "ban":
                await context.bot.ban_chat_member(chat_id, user_id)
            else:
                await context.bot.ban_chat_member(chat_id, user_id)
                await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        except TelegramError as e:
            logger.warning(f"اقدام روی کپچای منقضی ناموفق ({chat_id}/{user_id}): {e}")
            continue
        log_action(chat_id, SYSTEM_ACTOR_ID, None, "captcha_fail",
                  target_id=user_id, detail=f"مهلت تمام شد → "
                                            f"{'بن' if action == 'ban' else 'اخراج'}")
        if message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=f"{E('cross')} مهلت کپچا تمام شد و کاربر "
                         f"{'بن' if action == 'ban' else 'اخراج'} شد.",
                )
            except TelegramError:
                pass




async def handle_number_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    text = msg.text.strip().translate(_PERSIAN_DIGITS)     # اعداد فارسی هم قبول
    if not text.isdigit() or len(text) > len(str(RANGE_HARD_CAP)):
        return
    number = int(text)
    chat_id = update.effective_chat.id
    user = update.effective_user

    row = get_open_lottery(chat_id)
    if not row or row[1] != "active":
        return
    lottery_id, _, mn, mx = row
    mn = mn if mn is not None else DEFAULT_MIN_NUM
    mx = mx if mx is not None else DEFAULT_MAX_NUM
    if not (mn <= number <= mx):
        await reply(update, f"{E('warn')} عدد باید بین <code>{mn}</code> تا "
                            f"<code>{mx}</code> باشد.")
        return

    prev = None
    with db(commit=True) as conn:
        c = conn.cursor()
        try:
            c.execute(
                "INSERT INTO entries (lottery_id, user_id, username, full_name, number) "
                "VALUES (?, ?, ?, ?, ?)",
                (lottery_id, user.id, user.username, user.full_name, number),
            )
            conn.commit()
            await reply(update, f"{E('check')} عدد <code>{number}</code> برای شما ثبت شد.")
            return
        except sqlite3.IntegrityError:
            conn.rollback()
            c.execute("SELECT number FROM entries WHERE lottery_id=? AND user_id=?",
                      (lottery_id, user.id))
            prev = c.fetchone()

    if prev:
        await reply(update, f"{E('warn')} شما قبلاً عدد <code>{prev[0]}</code> را ثبت "
                            f"کرده‌اید. هر نفر فقط یک عدد.")
    else:
        await reply(update, f"{E('cross')} عدد <code>{number}</code> قبلاً گرفته شده. "
                            f"یک عدد دیگر امتحان کن.")


async def track_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        track_known_chat(update.effective_chat)


def _help_text(is_owner_flag: bool, admin_flag: bool) -> str:
    lines = [
        f"{E('sparkle')} <b>راهنمای بات</b>",
        DIVIDER,
        f"<b>— قرعه‌کشی —</b>",
        f"<b>/menu</b> — پنل دکمه‌ای",
        f"<b>/status</b> • <b>/myentry</b> • <b>/history</b> • <b>/leaderboard</b>",
    ]
    if admin_flag:
        lines += [
            f"<b>/startlottery</b> — شروع قرعه‌کشی",
            f"<b>/stoplottery</b> — بستن ثبت‌نام",
            f"<b>/pickwinner</b> [N] — مشخص کردن N برنده، پیش‌فرض ۳",
            f"<b>/cancellottery</b> — لغو قرعه‌کشی جاری",
            f"<b>/setrange</b> حداقل حداکثر — بازه‌ی عدد",
            f"<b>/export</b> — خروجی اکسل",
        ]
    lines += [
        "",
        f"<b>— آمار و لول —</b>",
        f"<b>/stats</b> • <b>/mystats</b> • <b>/topactive</b> • <b>/profile</b>",
        f"<b>/info</b> — اطلاعات کامل کاربر (با ریپلای)",
        "",
        f"<b>— گزارش اسکم / تبر —</b>",
        f"<b>/report</b> — ثبت گزارش با مدرک (در پیوی، قدم‌به‌قدم)",
        f"   در گروه: ریپلای روی پیام فرد + «گزارش اسکم»",
        f"<b>/scamcheck</b> آیدی — استعلام سابقه قبل از معامله",
        f"<b>/scamlist</b> — لیست سیاه اسکمرها",
    ]
    if admin_flag:
        lines += [
            f"<b>/scam</b> [دلیل] — ثبت فرد به‌عنوان اسکمر (با ریپلای)",
            f"   می‌توانی عکس مدرک را با کپشن <code>/scam دلیل</code> بفرستی",
            f"<b>/unscam</b> — حذف از لیست سیاه",
            f"<b>/reports</b> — گزارش‌های در انتظار • <b>/report_show</b> شماره",
        ]
    lines += ["", f"<b>— عمومی —</b>", f"<b>/myid</b>"]
    if admin_flag:
        lines += [
            f"<b>/admins</b> • <b>/groups</b>",
            f"<b>/emojis</b> • <b>/emojiid</b>",
        ]
    if is_owner_flag:
        lines += [f"<b>/addadmin</b> • <b>/removeadmin</b> • <b>/backup</b> (فقط مالک)",
                  f"<b>/adv</b> مقصد — ساخت پست تبلیغاتی با عکس و دکمه‌ی شیشه‌ای در کانال"]
    lines += [
        f"<b>/check</b> آی‌پی — چک آی‌پی (یا <code>Check!8.8.8.8</code>)",
        f"<b>/prices</b> — قیمت دلار، ارز و طلا "
        f"(<code>/prices refresh</code> برای دور زدن کش)",
    ]

    if admin_flag:
        lines += [
            "",
            f"<b>— مدیریت گروه (فقط ادمین) —</b>",
            f"<b>/rules</b> • <b>/setrules متن</b> • <b>/setwelcome متن</b>",
            f"<b>/warn</b> [دلیل] • <b>/unwarn</b> • <b>/warnings</b> • <b>/resetwarns</b>",
            f"<b>/mute</b> [دقیقه] • <b>/unmute</b> • <b>/muted</b>",
            f"<b>/kick</b> • <b>/ban</b> • <b>/unban</b>",
            f"<b>/del</b> — حذف پیام ریپلای‌شده • <b>/purge</b> — حذف گروهی",
            f"<b>/pin</b> [silent] • <b>/unpin</b>",
            f"<b>/whitelist_add</b> • <b>/whitelist_remove</b> • <b>/whitelist</b>",
            f"<b>/mgmtsettings</b> — تنظیمات با دکمه",
            f"<b>/toggle</b> antispam|antilink|welcome|night|antiforward",
            f"<b>/setmaxwarn</b> • <b>/setflood</b> تعداد ثانیه • <b>/setnight</b> شروع پایان",
            f"<b>/floodaction</b> mute|warn — واکنش به اسپم",
            f"<b>/antispam</b> — وضعیت و <u>تشخیص عیب</u> ضد اسپم",
        ]

    lines += [
        DIVIDER,
        f"{E('pin')} وقتی قرعه‌کشی باز باشد، کافی است عددی در بازه بفرستی.",
        f"{E('info')} با چت کردن، XP و لول می‌گیری! {E('level')}",
        f"{E('sparkle')} لازم نیست دستورها را با <code>/</code> بزنی — "
        f"مثلاً «وضعیت» یا «قیمت دلار»"
        + (" یا «ضد اسپم روشن»." if admin_flag else "."),
    ]
    return "\n".join(lines)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_flag = await is_admin(update, context)
    owner_flag = is_owner(update.effective_user.id)
    await reply(update, _help_text(owner_flag, admin_flag))


# ---------------------------------------------------------------------------
# هندلر دکمه‌های شیشه‌ای
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# سامانه‌ی گزارش اسکم / تبر
# ---------------------------------------------------------------------------
MAX_EVIDENCE = 10                 # حداکثر مستندات در هر گزارش
REPORT_KEY = "scam_report"        # کلید وضعیت گفت‌وگو در user_data

# مراحل ثبت گزارش در پیوی
ST_ACCUSED, ST_AMOUNT, ST_DESC, ST_EVIDENCE = "accused", "amount", "desc", "evidence"


def _parse_accused(raw: str):
    """«@user» یا «123456» یا «t.me/user» → (user_id یا None، متن خام)"""
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    # یوزرنیم/لینک را از متنِ خام می‌خوانیم تا «_» و «.» از بین نرود
    m = re.search(r"(?:t\.me/|telegram\.me/|@)([A-Za-z0-9_]{3,})", raw)
    if m:
        return None, "@" + m.group(1)
    digits = re.sub(r"[^0-9]", "", raw.translate(_PERSIAN_DIGITS))
    if digits and 5 <= len(digits) <= 15:
        return int(digits), raw
    return None, raw


# ---- لایه دیتابیس ----
def create_scam_report(reporter, accused_id, accused_raw, amount, description,
                       evidence: list, source_chat_id=None) -> int:
    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO scam_reports
            (reporter_id, reporter_name, reporter_username, accused_id, accused_raw,
             amount, description, evidence, status, source_chat_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (reporter.id, getattr(reporter, "full_name", None),
             getattr(reporter, "username", None), accused_id, accused_raw,
             amount, description, json.dumps(evidence or [], ensure_ascii=False),
             source_chat_id),
        )
        return c.lastrowid


def get_scam_report(report_id: int):
    with db() as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM scam_reports WHERE id=?", (report_id,))
        row = c.fetchone()
    return dict(row) if row else None


def set_report_status(report_id: int, status: str, reviewer_id: int, note: str = ""):
    with db(commit=True) as conn:
        conn.execute(
            "UPDATE scam_reports SET status=?, reviewed_by=?, "
            "reviewed_at=CURRENT_TIMESTAMP, review_note=? WHERE id=?",
            (status, reviewer_id, note, report_id),
        )


def list_pending_reports(limit: int = 15):
    with db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, reporter_id, accused_id, accused_raw, amount, created_at "
            "FROM scam_reports WHERE status='pending' ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return c.fetchall()


def add_scammer(user_id: int, username, full_name, report_id, note, added_by):
    with db(commit=True) as conn:
        conn.execute(
            """
            INSERT INTO scammers (user_id, username, full_name, report_id, note, added_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=COALESCE(excluded.username, scammers.username),
                full_name=COALESCE(excluded.full_name, scammers.full_name),
                report_id=excluded.report_id,
                note=excluded.note,
                added_by=excluded.added_by,
                added_at=CURRENT_TIMESTAMP
            """,
            (user_id, username, full_name, report_id, note, added_by),
        )
    _scammer_cache.pop(user_id, None)


def remove_scammer(user_id: int) -> bool:
    with db(commit=True) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM scammers WHERE user_id=?", (user_id,))
        removed = c.rowcount > 0
    _scammer_cache.pop(user_id, None)
    return removed


_scammer_cache: dict = {}
SCAMMER_CACHE_TTL = 120


def get_scammer(user_id: int):
    cached = _scammer_cache.get(user_id)
    if cached and time.time() - cached[0] < SCAMMER_CACHE_TTL:
        return cached[1]
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, username, full_name, report_id, note, added_at "
                  "FROM scammers WHERE user_id=?", (user_id,))
        row = c.fetchone()
    _scammer_cache[user_id] = (time.time(), row)
    return row


def list_scammers(limit: int = 30):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, username, full_name, note, added_at FROM scammers "
                  "ORDER BY added_at DESC LIMIT ?", (limit,))
        return c.fetchall()


def find_scammer_by_username(username: str):
    uname = (username or "").lstrip("@").lower()
    if not uname:
        return None
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, username, full_name, report_id, note, added_at "
                  "FROM scammers WHERE LOWER(username)=?", (uname,))
        return c.fetchone()


# ---- ابزارهای ارسال ----
def _reviewer_ids() -> list:
    return sorted(set(OWNER_IDS) | get_db_admin_ids())


def report_summary(rep: dict, short: bool = False) -> str:
    accused = rep.get("accused_raw") or (f"<code>{rep.get('accused_id')}</code>"
                                         if rep.get("accused_id") else "نامشخص")
    if rep.get("accused_id") and rep.get("accused_raw") and \
            str(rep["accused_id"]) not in str(rep["accused_raw"]):
        accused = f"{esc(rep['accused_raw'])} (<code>{rep['accused_id']}</code>)"
    else:
        accused = esc(strip_html(str(accused))) if "code>" not in str(accused) else accused
    status_fa = {"pending": f"{E('pending')} در انتظار بررسی",
                 "approved": f"{E('approve')} تایید شده (اسکمر)",
                 "rejected": f"{E('reject')} رد شده"}.get(rep.get("status"), rep.get("status"))
    evidence = json.loads(rep.get("evidence") or "[]")
    reporter = (f"@{esc(rep['reporter_username'])}" if rep.get("reporter_username")
                else esc(rep.get("reporter_name") or rep.get("reporter_id")))
    lines = [
        f"{E('report')} <b>گزارش اسکم #{rep['id']}</b>",
        DIVIDER,
        f"{E('scam')} متهم: {accused}",
        f"{E('id')} گزارش‌دهنده: {reporter} — <code>{rep['reporter_id']}</code>",
    ]
    if rep.get("amount"):
        lines.append(f"{E('card')} مبلغ/کارت: {esc(rep['amount'])}")
    if not short and rep.get("description"):
        lines.append(f"{E('scroll')} شرح: {esc(rep['description'])}")
    lines.append(f"{E('evidence')} مستندات: <code>{len(evidence)}</code> مورد")
    lines.append(f"{E('judge')} وضعیت: {status_fa}")
    lines.append(f"{E('clock')} زمان: <code>{esc(str(rep.get('created_at') or '')[:19])}</code>")
    return "\n".join(lines)


def review_keyboard(report_id: int, accused_id):
    rows = [[
        btn("approve", "تایید (اسکمر است)", callback_data=f"sc:ok:{report_id}"),
        btn("reject", "رد گزارش", callback_data=f"sc:no:{report_id}"),
    ]]
    if accused_id:
        rows.append([btn("evidence", "نمایش مستندات",
                         callback_data=f"sc:ev:{report_id}")])
    return InlineKeyboardMarkup(rows)


def scam_action_keyboard(user_id: int):
    """دکمه‌های اقدام سریع داخل گروه."""
    return InlineKeyboardMarkup([
        [btn("mute", "سکوت ۱ روز", callback_data=f"sc:mute:{user_id}:1440"),
         btn("ban", "بن کامل", callback_data=f"sc:ban:{user_id}:0")],
        [btn("trash", "حذف پیام‌های اخیر", callback_data=f"sc:clean:{user_id}:0"),
         btn("check", "لغو علامت اسکم", callback_data=f"sc:free:{user_id}:0")],
    ])


async def send_evidence_to(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                           evidence: list, prefix: str = ""):
    """مستندات گزارش را برای یک نفر می‌فرستد."""
    for idx, item in enumerate(evidence or [], start=1):
        kind = item.get("type")
        fid = item.get("file_id")
        cap = f"{prefix}{E('evidence')} مدرک {idx}/{len(evidence)}"
        try:
            if kind == "photo":
                await context.bot.send_photo(chat_id, fid, caption=cap)
            elif kind == "document":
                await context.bot.send_document(chat_id, fid, caption=cap)
            elif kind == "video":
                await context.bot.send_video(chat_id, fid, caption=cap)
            elif kind == "voice":
                await context.bot.send_voice(chat_id, fid, caption=cap)
            elif kind == "text":
                await context.bot.send_message(
                    chat_id, f"{cap}\n{esc(item.get('text', ''))}", parse_mode="HTML")
        except TelegramError as e:
            logger.warning(f"ارسال مدرک ناموفق به {chat_id}: {e}")
        await asyncio.sleep(0.2)


async def notify_reviewers(context: ContextTypes.DEFAULT_TYPE, report_id: int):
    """گزارش را به پیوی همه‌ی مالک‌ها و ادمین‌های ثبت‌شده می‌فرستد."""
    rep = get_scam_report(report_id)
    if not rep:
        return 0
    evidence = json.loads(rep.get("evidence") or "[]")
    text = report_summary(rep)
    sent = 0
    for admin_id in _reviewer_ids():
        try:
            await context.bot.send_message(
                admin_id, text, parse_mode="HTML",
                reply_markup=review_keyboard(report_id, rep.get("accused_id")),
            )
            await send_evidence_to(context, admin_id, evidence,
                                   prefix=f"#{report_id} • ")
            sent += 1
        except TelegramError as e:
            logger.warning(f"ارسال گزارش به ادمین {admin_id} ناموفق: {e}")
    return sent


# ---- جریان ثبت گزارش در پیوی ----
def _report_intro() -> str:
    return (
        f"{E('report')} <b>ثبت گزارش اسکم / تبر</b>\n"
        f"{DIVIDER}\n"
        f"مرحله ۱ از ۴ — <b>مشخصات فرد</b>\n"
        f"آیدی عددی یا یوزرنیم کسی که اسکمت کرده را بفرست.\n"
        f"مثال: <code>123456789</code> یا <code>@username</code>\n\n"
        f"{E('info')} اگر آیدی عددی‌اش را داری بهتر است (با ریپلای روی پیامش در گروه "
        f"و زدن <code>/id</code> پیدا می‌شود).\n"
        f"{E('cross')} لغو: <code>/cancel</code>"
    )


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع ثبت گزارش. در گروه با ریپلای، مستقیماً فرد را انتخاب می‌کند."""
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message

    # --- در گروه: با ریپلای روی پیام فرد ---
    if chat.type in GROUP_TYPES:
        target = await _reply_target(update)
        if not target:
            me = (await context.bot.get_me()).username
            await reply(update,
                        f"{E('report')} برای ثبت گزارش اسکم، روی پیام آن فرد ریپلای کن و "
                        f"بنویس «گزارش اسکم».\n"
                        f"{E('info')} یا در پیوی بات <code>/report</code> بزن تا "
                        f"قدم‌به‌قدم با مستندات ثبت شود: @{esc(me)}")
            return
        description = " ".join(context.args) if context.args else "گزارش از داخل گروه"
        evidence = _extract_evidence(msg) + _extract_evidence(msg.reply_to_message)
        rid = create_scam_report(
            user, target.id,
            f"@{target.username}" if target.username else (target.full_name or target.id),
            None, description, evidence, source_chat_id=chat.id,
        )
        sent = await notify_reviewers(context, rid)
        await reply(update,
                    f"{E('check')} گزارش شما با شماره <code>#{rid}</code> ثبت شد و برای "
                    f"<code>{sent}</code> ادمین ارسال شد.\n"
                    f"{E('info')} برای فرستادن عکس و مستندات بیشتر، در پیوی بات "
                    f"<code>/report</code> بزن.")
        return

    # --- در پیوی: جریان قدم‌به‌قدم ---
    context.user_data[REPORT_KEY] = {
        "step": ST_ACCUSED, "evidence": [], "accused_id": None,
        "accused_raw": "", "amount": "", "desc": "",
    }
    await reply(update, _report_intro())


def _extract_evidence(msg) -> list:
    """از یک پیام، مدرک قابل ذخیره استخراج می‌کند."""
    if not msg:
        return []
    out = []
    if msg.photo:
        out.append({"type": "photo", "file_id": msg.photo[-1].file_id})
    elif msg.document:
        out.append({"type": "document", "file_id": msg.document.file_id})
    elif msg.video:
        out.append({"type": "video", "file_id": msg.video.file_id})
    elif msg.voice:
        out.append({"type": "voice", "file_id": msg.voice.file_id})
    caption = (msg.caption or "").strip()
    if caption:
        out.append({"type": "text", "text": caption[:800]})
    elif not out and (msg.text or "").strip():
        out.append({"type": "text", "text": msg.text.strip()[:800]})
    return out


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.pop(REPORT_KEY, None):
        await reply(update, f"{E('check')} ثبت گزارش لغو شد.")
    else:
        await reply(update, f"{E('info')} کار نیمه‌تمامی برای لغو وجود ندارد.")


async def report_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """جریان چندمرحله‌ای ثبت گزارش — فقط در پیوی و وقتی گزارشی در حال ثبت است."""
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or chat.type != "private" or not msg:
        return
    state = context.user_data.get(REPORT_KEY)
    if not state:
        return

    text = (msg.text or msg.caption or "").strip()
    norm = _normalize_command_text(text).lower()
    if norm in ("لغو", "انصراف", "بیخیال", "/cancel", "cancel"):
        context.user_data.pop(REPORT_KEY, None)
        await reply(update, f"{E('check')} ثبت گزارش لغو شد.")
        raise ApplicationHandlerStop

    step = state["step"]

    if step == ST_ACCUSED:
        accused_id, raw = _parse_accused(text)
        if not raw:
            await reply(update, f"{E('warn')} آیدی عددی یا یوزرنیم را بفرست.")
            raise ApplicationHandlerStop
        state["accused_id"], state["accused_raw"] = accused_id, raw
        state["step"] = ST_AMOUNT
        await reply(update,
                    f"{E('check')} فرد ثبت شد: <b>{esc(raw)}</b>\n"
                    f"{DIVIDER}\n"
                    f"مرحله ۲ از ۴ — <b>مبلغ و شماره کارت</b>\n"
                    f"مبلغ ضرر و شماره کارت/حسابی که پول به آن واریز شده را بنویس.\n"
                    f"اگر ندارد بنویس <code>ندارم</code>.")
        raise ApplicationHandlerStop

    if step == ST_AMOUNT:
        state["amount"] = "" if norm in ("ندارم", "ندارد", "نه", "-") else text[:300]
        state["step"] = ST_DESC
        await reply(update,
                    f"{DIVIDER}\n"
                    f"مرحله ۳ از ۴ — <b>شرح ماجرا</b>\n"
                    f"در چند خط بنویس دقیقاً چه اتفاقی افتاده (تاریخ، نحوه‌ی معامله، "
                    f"قول‌هایی که داده و عمل نکرده).")
        raise ApplicationHandlerStop

    if step == ST_DESC:
        if len(text) < 10:
            await reply(update, f"{E('warn')} کمی کامل‌تر توضیح بده (حداقل ۱۰ حرف).")
            raise ApplicationHandlerStop
        state["desc"] = text[:2000]
        state["step"] = ST_EVIDENCE
        await reply(update,
                    f"{DIVIDER}\n"
                    f"مرحله ۴ از ۴ — <b>مستندات</b>\n"
                    f"{E('evidence')} حالا عکس‌ها را بفرست: اسکرین چت، رسید بانکی، "
                    f"عکس شماره کارت و هر مدرک دیگری.\n"
                    f"{E('info')} تا <code>{MAX_EVIDENCE}</code> مدرک می‌توانی بفرستی.\n"
                    f"{E('check')} بعد از تمام شدن بنویس: <code>تمام</code>")
        raise ApplicationHandlerStop

    if step == ST_EVIDENCE:
        if norm in ("تمام", "تموم", "پایان", "ارسال", "done", "ok", "باشه"):
            if not state["evidence"]:
                await reply(update, f"{E('warn')} حداقل یک مدرک بفرست، بعد بنویس «تمام».")
                raise ApplicationHandlerStop
            user = update.effective_user
            rid = create_scam_report(
                user, state["accused_id"], state["accused_raw"], state["amount"],
                state["desc"], state["evidence"],
            )
            context.user_data.pop(REPORT_KEY, None)
            sent = await notify_reviewers(context, rid)
            await reply(update,
                        f"{E('check')} <b>گزارش شما ثبت شد.</b>\n"
                        f"{DIVIDER}\n"
                        f"شماره پیگیری: <code>#{rid}</code>\n"
                        f"برای <code>{sent}</code> ادمین ارسال شد.\n"
                        f"{E('info')} نتیجه‌ی بررسی به همین‌جا اطلاع داده می‌شود.")
            raise ApplicationHandlerStop

        items = _extract_evidence(msg)
        if not items:
            await reply(update, f"{E('warn')} عکس یا فایل بفرست، یا بنویس «تمام».")
            raise ApplicationHandlerStop
        space = MAX_EVIDENCE - len(state["evidence"])
        state["evidence"].extend(items[:max(0, space)])
        count = len(state["evidence"])
        if count >= MAX_EVIDENCE:
            await reply(update, f"{E('warn')} به سقف <code>{MAX_EVIDENCE}</code> مدرک "
                                f"رسیدی. بنویس «تمام» تا ارسال شود.")
        else:
            await reply(update, f"{E('check')} مدرک ثبت شد ({count}/{MAX_EVIDENCE}). "
                                f"باز هم بفرست یا بنویس «تمام».")
        raise ApplicationHandlerStop


# ---- دستورهای مدیریتی اسکم ----
async def scam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """در گروه: ریپلای روی پیام فرد + مستندات → علامت‌گذاری اسکمر و دکمه‌های اقدام."""
    if not await require_group(update) or not await require_admin(update, context):
        return
    msg = update.effective_message
    target = await _reply_target(update)
    if not target:
        target = await _target_or_id(update, context)
    if not target:
        await reply(update, f"{E('info')} روی پیام فرد ریپلای کن و بنویس "
                            f"<code>/scam دلیل</code> — می‌توانی همان لحظه عکس مدرک را "
                            f"هم با همین کپشن بفرستی.")
        return
    if not await _guard_target(update, context, target, "علامت اسکم"):
        return

    note = " ".join(context.args) if context.args else "ثبت‌شده توسط ادمین گروه"
    evidence = _extract_evidence(msg)
    if msg.reply_to_message:
        evidence += _extract_evidence(msg.reply_to_message)

    rid = create_scam_report(
        update.effective_user, target.id,
        f"@{target.username}" if target.username else (target.full_name or target.id),
        None, note, evidence, source_chat_id=update.effective_chat.id,
    )
    set_report_status(rid, "approved", update.effective_user.id, "تایید مستقیم ادمین")
    add_scammer(target.id, target.username, getattr(target, "full_name", None),
                rid, note, update.effective_user.id)

    await reply(update,
                f"{E('siren')} <b>هشدار اسکمر</b>\n"
                f"{DIVIDER}\n"
                f"{E('scam')} {mention_link(target.id, getattr(target, 'full_name', target.id))} "
                f"به‌عنوان <b>تبرزن/اسکمر</b> ثبت شد.\n"
                f"{E('id')} آیدی: <code>{target.id}</code>\n"
                f"{E('scroll')} دلیل: {esc(note)}\n"
                f"{E('report')} پرونده: <code>#{rid}</code>\n"
                f"{DIVIDER}\n"
                f"{E('info')} اقدام را از دکمه‌های زیر انتخاب کن:",
                reply_markup=scam_action_keyboard(target.id))


async def scamcheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استعلام سابقه‌ی یک نفر — برای همه‌ی اعضا."""
    target = await _reply_target(update)
    row = None
    label = ""
    if target:
        row = get_scammer(target.id)
        label = mention_link(target.id, target.full_name)
    elif context.args:
        accused_id, raw = _parse_accused(" ".join(context.args))
        label = esc(raw)
        row = get_scammer(accused_id) if accused_id else find_scammer_by_username(raw)
    else:
        await reply(update, f"{E('info')} فرمت: <code>/scamcheck آیدی یا @یوزرنیم</code> "
                            f"یا روی پیام فرد ریپلای کن.")
        return

    if not row:
        await reply(update, f"{E('check')} {label} در لیست سیاه نیست.\n"
                            f"{E('warn')} نبودن در لیست به‌معنی سالم بودن قطعی نیست — "
                            f"معامله را با واسطه انجام بده.")
        return
    uid, uname, fname, rid, note, added = row
    await reply(update,
                f"{E('siren')} <b>این فرد در لیست سیاه است!</b>\n"
                f"{DIVIDER}\n"
                f"{E('id')} آیدی: <code>{uid}</code>\n"
                f"{E('id')} یوزرنیم: {('@' + esc(uname)) if uname else '-'}\n"
                f"{E('scroll')} دلیل: {esc(note or '-')}\n"
                f"{E('report')} پرونده: <code>#{rid}</code>\n"
                f"{E('clock')} ثبت: <code>{esc(str(added)[:19])}</code>")


async def scamlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_scammers(30)
    if not rows:
        await reply(update, f"{E('check')} لیست سیاه خالی است.")
        return
    lines = [f"{E('blacklist')} <b>لیست سیاه اسکمرها</b>", DIVIDER]
    for uid, uname, fname, note, added in rows:
        label = f"@{esc(uname)}" if uname else esc(fname or uid)
        lines.append(f"{E('scam')} {label} — <code>{uid}</code>"
                     f"{' • ' + esc((note or '')[:40]) if note else ''}")
    lines.append(DIVIDER)
    lines.append(f"{E('info')} استعلام: <code>/scamcheck آیدی</code>")
    await _reply_long(update, "\n".join(lines))


async def unscam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target = await _target_or_id(update, context)
    if not target:
        await reply(update, f"{E('info')} روی پیام فرد ریپلای کن یا آیدی عددی بده.")
        return
    if remove_scammer(target.id):
        await reply(update, f"{E('check')} <code>{target.id}</code> از لیست سیاه حذف شد.")
    else:
        await reply(update, f"{E('info')} این فرد در لیست سیاه نبود.")


async def reports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لیست گزارش‌های در انتظار بررسی (ادمین)."""
    if not await require_admin(update, context):
        return
    rows = list_pending_reports()
    if not rows:
        await reply(update, f"{E('check')} گزارش در انتظار بررسی وجود ندارد.")
        return
    lines = [f"{E('judge')} <b>گزارش‌های در انتظار بررسی</b>", DIVIDER]
    for rid, reporter, accused_id, accused_raw, amount, created in rows:
        lines.append(
            f"{E('report')} <code>#{rid}</code> — متهم: {esc(accused_raw or accused_id)}"
            f" • گزارش‌دهنده: <code>{reporter}</code>\n"
            f"   {E('clock')} {esc(str(created)[:19])} • "
            f"بازکردن: <code>/report_show {rid}</code>"
        )
    await _reply_long(update, "\n".join(lines))


async def report_show_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش کامل یک گزارش همراه با مستندات و دکمه‌های داوری (ادمین)."""
    if not await require_admin(update, context):
        return
    if not context.args:
        await reply(update, f"{E('info')} فرمت: <code>/report_show شماره_گزارش</code>")
        return
    try:
        rid = int(str(context.args[0]).translate(_PERSIAN_DIGITS).lstrip("#"))
    except ValueError:
        await reply(update, f"{E('cross')} شماره نامعتبر.")
        return
    rep = get_scam_report(rid)
    if not rep:
        await reply(update, f"{E('cross')} گزارشی با این شماره پیدا نشد.")
        return
    await reply(update, report_summary(rep),
                reply_markup=review_keyboard(rid, rep.get("accused_id")))
    await send_evidence_to(context, update.effective_chat.id,
                           json.loads(rep.get("evidence") or "[]"), prefix=f"#{rid} • ")


# ---- هشدار خودکار وقتی اسکمر در گروه پیام می‌دهد ----
async def maybe_warn_scammer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user):
    row = get_scammer(user.id)
    if not row:
        return
    if not _cooldown_ok((chat_id, user.id, "scamwarn"), 3600):
        return
    await announce(context, chat_id,
                   f"{E('siren')} <b>هشدار</b>\n"
                   f"{mention_link(user.id, user.full_name)} "
                   f"(<code>{user.id}</code>) در لیست سیاه اسکمرهای این بات است.\n"
                   f"{E('scroll')} دلیل: {esc((row[4] or '-')[:120])}\n"
                   f"{E('info')} برای جزئیات: <code>/scamcheck {user.id}</code>")


# ---- هندلر دکمه‌های اسکم ----
async def scam_button(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list):
    query = update.callback_query
    action = parts[1] if len(parts) > 1 else ""
    user_id = query.from_user.id

    # ---- داوری گزارش (فقط ادمین/مالک) ----
    if action in ("ok", "no", "ev"):
        if user_id not in _reviewer_ids():
            await query.answer("⛔ فقط ادمین‌های بات اجازه دارند.", show_alert=True)
            return
        try:
            rid = int(parts[2])
        except (IndexError, ValueError):
            return
        rep = get_scam_report(rid)
        if not rep:
            await query.answer("گزارش پیدا نشد.", show_alert=True)
            return

        if action == "ev":
            await query.answer("در حال ارسال مستندات…")
            await send_evidence_to(context, query.message.chat.id,
                                   json.loads(rep.get("evidence") or "[]"),
                                   prefix=f"#{rid} • ")
            return

        if rep.get("status") != "pending":
            await query.answer("این گزارش قبلاً بررسی شده.", show_alert=True)
            return

        if action == "ok":
            set_report_status(rid, "approved", user_id, "تایید توسط ادمین")
            accused_id = rep.get("accused_id")
            if accused_id:
                add_scammer(accused_id, None, rep.get("accused_raw"), rid,
                            (rep.get("description") or "")[:200], user_id)
            rep = get_scam_report(rid)
            extra = ""
            if accused_id:
                extra = f"\n{E('blacklist')} به لیست سیاه اضافه شد."
            else:
                extra = (f"\n{E('warn')} آیدی عددی متهم مشخص نبود، پس فقط پرونده "
                         f"تایید شد. با <code>/scam</code> در گروه علامتش بزن.")
            await safe_edit(query, report_summary(rep) + extra,
                            reply_markup=(scam_action_keyboard(accused_id)
                                          if accused_id else None))
            try:
                await context.bot.send_message(
                    rep["reporter_id"],
                    f"{E('approve')} گزارش <code>#{rid}</code> شما بررسی و "
                    f"<b>تایید</b> شد. ممنون از گزارشت.",
                    parse_mode="HTML")
            except TelegramError:
                pass
        else:
            set_report_status(rid, "rejected", user_id, "رد توسط ادمین")
            rep = get_scam_report(rid)
            await safe_edit(query, report_summary(rep))
            try:
                await context.bot.send_message(
                    rep["reporter_id"],
                    f"{E('reject')} گزارش <code>#{rid}</code> شما بررسی شد و "
                    f"مدارک برای تایید کافی نبود.",
                    parse_mode="HTML")
            except TelegramError:
                pass
        return

    # ---- اقدام روی اسکمر داخل گروه ----
    if action in ("mute", "ban", "clean", "free"):
        chat_id = query.message.chat.id
        if not await is_admin_of(context.bot, user_id, chat_id):
            await query.answer("⛔ فقط ادمین اجازه دارد.", show_alert=True)
            return
        try:
            target_id = int(parts[2])
            minutes = int(parts[3]) if len(parts) > 3 else 0
        except (IndexError, ValueError):
            return

        if action == "free":
            remove_scammer(target_id)
            unmute_user(chat_id, target_id)
            try:
                await context.bot.restrict_chat_member(
                    chat_id, target_id, permissions=UNMUTE_PERMISSIONS)
            except TelegramError:
                pass
            await query.answer("علامت اسکم برداشته شد ✅")
            await safe_edit(query, f"{E('check')} علامت اسکم <code>{target_id}</code> "
                                   f"برداشته و محدودیتش رفع شد.")
            return

        if action == "clean":
            clear_message_log(chat_id, target_id)
            await query.answer("لاگ پیام‌ها پاک شد ✅")
            return

        if action == "mute":
            until = _utcnow() + timedelta(minutes=minutes or 1440)
            mute_user(chat_id, target_id, until, "اسکم")
            ok = True
            try:
                await context.bot.restrict_chat_member(
                    chat_id, target_id, permissions=MUTE_PERMISSIONS, until_date=until)
            except TelegramError as e:
                ok = False
                logger.warning(f"سایلنت اسکمر ناموفق: {e}")
            await query.answer("سایلنت شد 🔇" if ok else "دسترسی محدودسازی نداری!",
                               show_alert=not ok)
            await safe_edit(query,
                            f"{E('mute')} <code>{target_id}</code> به دلیل اسکم برای "
                            f"<b>{human_duration(minutes or 1440)}</b> سایلنت شد.",
                            reply_markup=scam_action_keyboard(target_id))
            return

        if action == "ban":
            try:
                await context.bot.ban_chat_member(chat_id, target_id)
                await query.answer("بن شد 🚫")
                await safe_edit(query,
                                f"{E('ban')} <code>{target_id}</code> به دلیل اسکم "
                                f"از گروه بن شد.")
            except TelegramError as e:
                await query.answer(f"بن ناموفق: {e}", show_alert=True)
            return


ADMIN_ONLY_ACTIONS = {"start", "stop", "pick", "export", "rangemenu",
                      "setrange", "mgmtsettings", "tog", "diag"}


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except TelegramError:
        pass

    parts = (query.data or "").split(":")
    if not parts or not parts[0]:
        return
    kind = parts[0]
    in_private = query.message.chat.type == "private"
    user_id = query.from_user.id

    # ---- سامانه‌ی اسکم ----
    if kind == "sc":
        await scam_button(update, context, parts)
        return

    # ---- کپچای اعضای جدید ----
    if kind == "cap":
        await captcha_button(update, context, parts)
        return

    if kind == "noop":
        return

    # ---- منوها ----
    if kind == "m":
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "groups":
            rows, kb = group_list_keyboard()
            if not rows:
                await safe_edit(query, f"{E('info')} هیچ گروهی شناسایی نشده.")
                return
            await safe_edit(query, f"{E('menu')} <b>پنل مدیریت</b>\nیک گروه را انتخاب کن:",
                            reply_markup=kb)
        elif sub == "grp" and len(parts) > 2:
            try:
                target = int(parts[2])
            except ValueError:
                return
            admin = await is_admin_of(context.bot, user_id, target)
            await safe_edit(query,
                            f"{E('menu')} <b>پنل مدیریت گروه</b>\n"
                            f"آیدی گروه: <code>{target}</code>",
                            reply_markup=action_keyboard(target, admin, True))
        return

    if kind != "a" or len(parts) < 3:
        return

    action = parts[1]
    try:
        target = int(parts[2])
    except ValueError:
        return

    admin = await is_admin_of(context.bot, user_id, target)
    if action in ADMIN_ONLY_ACTIONS and not admin:
        await query.answer(text="⛔ فقط ادمین یا مالک اجازه دارد.", show_alert=True)
        return

    kb = lambda: action_keyboard(target, admin, in_private)  # noqa: E731

    if action == "start":
        ok, text = core_start_lottery(target)
        if ok:
            await announce(context, target, text)
            text = (f"{E('check')} قرعه‌کشی توی گروه هدف شروع شد." if in_private
                    else f"{E('check')} قرعه‌کشی شروع شد و اعلام شد.")
        await safe_edit(query, text, reply_markup=kb())

    elif action == "stop":
        ok, text = core_stop_lottery(target)
        if ok:
            await announce(context, target, text)
            text = (f"{E('check')} ثبت‌نام توی گروه هدف بسته شد." if in_private
                    else f"{E('check')} ثبت‌نام بسته شد و اعلام شد.")
        await safe_edit(query, text, reply_markup=kb())

    elif action == "pick":
        try:
            n = int(parts[3]) if len(parts) > 3 else 3
        except ValueError:
            n = 3
        ok, text = core_pick_winner(target, n)
        if ok:
            try:
                await context.bot.send_dice(chat_id=target, emoji="🎰")
                await asyncio.sleep(3)
            except TelegramError as e:
                logger.warning(f"ارسال انیمیشن اسلات ناموفق: {e}")
            await announce(context, target, text)
            text = (f"{E('check')} نتیجه توی گروه هدف اعلام شد." if in_private
                    else f"{E('check')} برنده(ها) مشخص و اعلام شدند.")
        await safe_edit(query, text, reply_markup=kb())

    elif action == "prices":
        try:
            text = await _build_market_prices()
        except Exception as e:
            logger.exception(f"خطا در callback قیمت بازار: {e}")
            text = f"{E('cross')} دریافت قیمت‌های بازار ناموفق بود."
        await safe_edit(query, text, reply_markup=kb())

    elif action == "status":
        await safe_edit(query, core_status_text(target), reply_markup=kb())

    elif action == "hist":
        await safe_edit(query, core_history_text(target), reply_markup=kb())

    elif action == "leaderboard":
        await safe_edit(query, core_leaderboard_text(target), reply_markup=kb())

    elif action == "myentry":
        await safe_edit(query, core_my_entry_text(target, user_id), reply_markup=kb())

    elif action in ("profile", "mystats"):
        title = "پروفایل من" if action == "profile" else "آمار من"
        await safe_edit(query, render_profile_text(target, query.from_user, title),
                        reply_markup=kb())

    elif action == "topactive":
        top = get_top_users(target, limit=10, order_by="xp")
        if not top:
            text = f"{E('info')} هنوز آماری ثبت نشده."
        else:
            lines = [f"{E('fire')} <b>فعال‌ترین اعضا</b>", DIVIDER]
            for idx, (uid, uname, fname, mcount, xp, level) in enumerate(top, start=1):
                lines.append(f"{medal_for(idx)} {mention_of(uname, fname)} — "
                             f"لول <code>{level}</code> • <code>{xp}</code> XP")
            text = "\n".join(lines)
        await safe_edit(query, text, reply_markup=kb())

    elif action == "mgmtsettings":
        await safe_edit(query, mgmt_settings_text(target),
                        reply_markup=mgmt_menu_keyboard(target))

    elif action == "tog" and len(parts) >= 4:
        field = parts[3]
        if field not in ("antispam", "antilink", "welcome", "night_mode",
                         "antiforward", "captcha"):
            return
        mgmt = get_group_mgmt(target)
        new_val = not mgmt[field]
        update_group_mgmt(target, **{field: new_val})
        log_action(target, user_id, getattr(query.from_user, "full_name", None),
                  "toggle_setting", detail=f"{field} → {'on' if new_val else 'off'}")
        await query.answer("روشن شد ✅" if new_val else "خاموش شد 🔴")
        await safe_edit(query, mgmt_settings_text(target),
                        reply_markup=mgmt_menu_keyboard(target))

    elif action == "diag":
        text = await build_antispam_report(context, target)
        await safe_edit(query, text, reply_markup=kb())

    elif action == "rangemenu":
        cur_mn, cur_mx = get_group_range(target)
        await safe_edit(query,
                        f"{E('range')} <b>تنظیم بازه‌ی عدد</b>\n"
                        f"بازه‌ی فعلی: <code>{cur_mn}</code> تا <code>{cur_mx}</code>\n\n"
                        f"یکی از بازه‌های آماده را انتخاب کن، یا:\n"
                        f"<code>/setrange {target} حداقل حداکثر</code>",
                        reply_markup=range_menu_keyboard(target))

    elif action == "setrange" and len(parts) >= 5:
        try:
            mn, mx = int(parts[3]), int(parts[4])
        except ValueError:
            return
        set_group_range(target, mn, mx)
        await query.answer(f"بازه به {mn}-{mx} تغییر کرد ✅")
        await safe_edit(query,
                        f"{E('range')} <b>تنظیم بازه‌ی عدد</b>\n"
                        f"بازه‌ی فعلی: <code>{mn}</code> تا <code>{mx}</code>\n\n"
                        f"یکی از بازه‌های آماده را انتخاب کن، یا:\n"
                        f"<code>/setrange {target} حداقل حداکثر</code>",
                        reply_markup=range_menu_keyboard(target))

    elif action == "backmain":
        await safe_edit(query,
                        f"{E('menu')} <b>پنل مدیریت گروه</b>\n"
                        f"آیدی گروه: <code>{target}</code>",
                        reply_markup=kb())

    elif action == "export":
        tmp_path, lottery_id = core_build_export_workbook(target)
        if tmp_path == "NO_OPENPYXL":
            await query.answer("openpyxl نصب نیست.", show_alert=True)
            return
        if not tmp_path:
            await query.answer("هیچ شرکت‌کننده‌ای برای خروجی نیست.", show_alert=True)
            return
        try:
            with open(tmp_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=query.message.chat.id,
                    document=InputFile(f, filename=f"lottery_{lottery_id}.xlsx"),
                    caption=f"{E('doc')} خروجی قرعه‌کشی شماره {lottery_id}",
                )
        except TelegramError as e:
            logger.warning(f"ارسال فایل ناموفق: {e}")
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# اجرای دستورها بدون / و با عبارت‌های فارسی
# ---------------------------------------------------------------------------
_ARABIC_FIX = str.maketrans({
    "ي": "ی", "ى": "ی", "ئ": "ی", "ك": "ک", "ھ": "ه", "ۀ": "ه", "ة": "ه",
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ؤ": "و",
})
# اعراب، کشیده و علامت‌های نامرئی
_DIACRITICS_RE = re.compile(r"[\u064B-\u0652\u0640\u200b-\u200f\ufeff]")
# علائم نگارشی که کاربر ممکن است ته دستور بگذارد
_PUNCT_RE = re.compile(r"[!?.,؛؟!٬,\*\-_=+~«»\"'()\[\]{}:]+")


def _normalize_command_text(text: str) -> str:
    """یکسان‌سازی حروف عربی/فارسی، حذف اعراب، نیم‌فاصله، علائم و فاصله‌های تکراری.
    این تابع هم روی ورودی کاربر و هم روی خودِ alias‌ها اجرا می‌شود تا
    «سکوت»، «سُکوت»، «سکوت‌کن» و «سکوت کن!» همه یکی حساب شوند."""
    text = (text or "").strip()
    text = text.replace("\u200c", " ")        # نیم‌فاصله → فاصله
    text = _DIACRITICS_RE.sub("", text)
    text = text.translate(_ARABIC_FIX)
    text = text.translate(_PERSIAN_DIGITS)
    text = _PUNCT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


# واحدهای زمانی فارسی برای دستورهایی مثل «سکوت کن ۲ ساعت»
_TIME_UNITS = {
    "دقیقه": 1, "دقیقه‌ای": 1, "دقیقه ای": 1, "د": 1, "min": 1, "m": 1,
    "ساعت": 60, "ساعته": 60, "h": 60, "hour": 60,
    "روز": 1440, "روزه": 1440, "d": 1440, "day": 1440,
    "هفته": 10080, "هفته‌ای": 10080, "week": 10080, "w": 10080,
}


def parse_duration_minutes(args, default: int = 10) -> int:
    """«10» / «۲ ساعت» / «3روز» / «30m» → دقیقه."""
    if not args:
        return default
    raw = _normalize_command_text(" ".join(str(a) for a in args)).lower()
    m = re.search(r"(\d+)\s*([a-zآ-ی‌ ]*)", raw)
    if not m:
        return default
    value = int(m.group(1))
    unit_word = (m.group(2) or "").strip().split(" ")[0]
    factor = _TIME_UNITS.get(unit_word, 1)
    return max(1, min(60 * 24 * 30, value * factor))


TEXT_COMMAND_ALIASES = {
    "menu": ["منو", "پنل", "پنل مدیریت"],
    "help": ["راهنما", "کمک"],
    "startlottery": ["شروع قرعه کشی", "شروع قرعه", "شروع"],
    "stoplottery": ["بستن ثبت نام", "پایان ثبت نام", "توقف قرعه"],
    "pickwinner": ["تعیین برنده", "قرعه بکش", "مشخص کردن برنده"],
    "cancellottery": ["لغو قرعه کشی", "لغو قرعه"],
    "setrange": ["تنظیم بازه", "بازه عدد"],
    "status": ["وضعیت"],
    "myentry": ["عدد من"],
    "export": ["خروجی اکسل", "خروجی"],
    "history": ["تاریخچه"],
    "leaderboard": ["برترین ها", "لیدربورد"],
    "groups": ["لیست گروه ها", "گروه ها"],
    "myid": ["آیدی من", "ایدی من"],
    "admins": ["لیست ادمین ها", "ادمین ها"],
    "addadmin": ["اضافه کردن ادمین", "ادمین کردن"],
    "removeadmin": ["حذف ادمین", "برداشتن ادمین"],
    "emojis": ["لیست ایموجی ها", "ایموجی ها"],
    "check": ["چک آیپی", "چک آی پی"],
    "prices": ["قیمت", "قیمت ها", "قیمت بازار", "نرخ ارز", "قیمت دلار", "قیمت طلا"],
    "stats": ["آمار گروه"],
    "mystats": ["آمار من"],
    "topactive": ["فعال ترین ها"],
    "profile": ["پروفایل"],
    "info": ["اطلاعات کاربر", "مشخصات"],
    "rules": ["قوانین"],
    "setrules": ["تنظیم قوانین"],
    "setwelcome": ["تنظیم خوش آمد گویی", "تنظیم خوش آمد"],
    "warn": ["اخطار بده", "اخطار"],
    "unwarn": ["حذف اخطار", "کم کردن اخطار"],
    "warnings": ["تعداد اخطار"],
    "resetwarns": ["پاک کردن اخطار ها", "ریست اخطار"],
    "mute": [
        "سایلنت کن", "سایلنتش کن", "سایلنت", "میوت کن", "میوتش کن", "میوت",
        "سکوت", "سکوت کن", "سکوتش کن", "سکوتش کنید", "به سکوت",
        "ساکت", "ساکت کن", "ساکتش کن", "ساکتش کنید", "خفه کن",
        "بی صدا کن", "بیصدا کن", "محدودش کن", "محدود کن", "ببند دهنش",
    ],
    "unmute": [
        "آن سایلنت", "انسایلنت", "آنسایلنت", "آنمیوت", "انمیوت", "ان میوت",
        "رفع سایلنت", "رفع سکوت", "رفع میوت", "لغو سکوت", "لغو سایلنت",
        "باز کن صداش", "آزادش کن", "ازادش کن", "سکوت بردار", "از سکوت درش بیار",
    ],
    "muted": ["لیست سایلنت", "سایلنت ها"],
    "kick": ["اخراج کن", "اخراج"],
    "ban": ["بن کن", "بن"],
    "unban": ["آنبن کن", "آنبن", "رفع بن"],
    "del": ["حذف پیام", "پاک کن"],
    "purge": ["پاکسازی", "حذف گروهی"],
    "pin": ["پین کن", "سنجاق"],
    "unpin": ["حذف پین", "برداشتن پین"],
    "whitelist_add": ["لیست سفید اضافه", "معاف کن"],
    "whitelist_remove": ["لیست سفید حذف", "حذف معافیت"],
    "whitelist": ["لیست سفید"],
    "mgmtsettings": ["تنظیمات مدیریت گروه", "تنظیمات مدیریت", "تنظیمات"],
    "toggle": ["تغییر وضعیت"],
    "setmaxwarn": ["تنظیم حداکثر اخطار"],
    "setflood": ["تنظیم فلاد"],
    "setnight": ["تنظیم حالت شب"],
    "floodaction": ["واکنش اسپم"],
    "antispamon": ["ضد اسپم روشن", "آنتی اسپم روشن", "روشن کردن ضد اسپم"],
    "antispamoff": ["ضد اسپم خاموش", "آنتی اسپم خاموش", "خاموش کردن ضد اسپم"],
    "antilinkon": ["ضد لینک روشن", "روشن کردن ضد لینک"],
    "antilinkoff": ["ضد لینک خاموش", "خاموش کردن ضد لینک"],
    "antispamstatus": ["ضد اسپم", "آنتی اسپم", "وضعیت ضد اسپم", "تست ضد اسپم"],
    "report": ["گزارش اسکم", "گزارش تبر", "گزارش کلاهبرداری", "ثبت گزارش", "شکایت"],
    "scam": ["اسکمر", "تبرزن", "تبر زد", "اسکم کرد", "ثبت اسکمر", "علامت اسکم"],
    "scamcheck": ["چک اسکم", "سابقه", "استعلام", "چک تبر", "سابقه اسکم"],
    "scamlist": ["لیست اسکمر ها", "لیست سیاه", "بلک لیست"],
    "unscam": ["حذف اسکمر", "پاک کردن اسکم", "رفع اسکم"],
    "reports": ["گزارش ها", "لیست گزارش ها", "گزارش های در انتظار"],
    "cancel": ["لغو", "انصراف", "بیخیال"],
    "adv": ["تبلیغ", "ارسال تبلیغ", "پست تبلیغاتی", "تبلیغ در کانال"],
}

ADMIN_ONLY_TEXT_COMMANDS = {
    "startlottery", "stoplottery", "pickwinner", "cancellottery", "setrange",
    "export", "groups", "addadmin", "removeadmin", "emojis", "setrules",
    "setwelcome", "warn", "unwarn", "resetwarns", "mute", "unmute", "muted",
    "kick", "ban", "unban", "del", "purge", "pin", "unpin",
    "whitelist_add", "whitelist_remove", "whitelist",
    "mgmtsettings", "toggle", "setmaxwarn", "setflood", "setnight", "floodaction",
    "antispamon", "antispamoff", "antilinkon", "antilinkoff",
    "scam", "unscam", "reports", "adv",
}


def _build_alias_lookup():
    lookup = []
    for cmd, aliases in TEXT_COMMAND_ALIASES.items():
        for alias in set(aliases) | {cmd}:
            norm = _normalize_command_text(alias).lower()
            if norm:
                lookup.append((cmd, norm, len(norm.split(" "))))
    lookup.sort(key=lambda item: -item[2])   # عبارت‌های طولانی‌تر اول
    return lookup


_ALIAS_LOOKUP = _build_alias_lookup()
COMMAND_HANDLERS_MAP: dict = {}     # پایین‌تر پر می‌شود


async def text_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اجازه می‌دهد دستورها بدون / و با عبارت فارسی اجرا شوند."""
    msg = update.effective_message
    if not msg or not msg.text:
        return
    raw = msg.text
    if raw.startswith("/"):
        return

    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or user.is_bot:
        return

    norm_full = _normalize_command_text(raw)
    if not norm_full:
        return
    lower_words = norm_full.lower().split(" ")

    matched_cmd, matched_wc = None, 0
    for cmd, alias, wc in _ALIAS_LOOKUP:
        if wc > len(lower_words):
            continue
        if " ".join(lower_words[:wc]) == alias:
            matched_cmd, matched_wc = cmd, wc
            break

    if not matched_cmd:
        return
    handler = COMMAND_HANDLERS_MAP.get(matched_cmd)
    if handler is None:
        return

    if matched_cmd in ADMIN_ONLY_TEXT_COMMANDS and chat.type in GROUP_TYPES:
        if not await is_admin(update, context):
            return

    context.args = raw.strip().split()[matched_wc:]
    track_known_chat(chat)

    try:
        await handler(update, context)
    except Exception as e:
        logger.exception(f"خطا در اجرای دستور متنی «{matched_cmd}»: {e}")
    # عمداً ApplicationHandlerStop نمی‌دهیم تا XP و آمار هم ثبت شود.


CAPTION_COMMANDS = {
    "scam": "scam", "report": "report", "warn": "warn", "mute": "mute", "ban": "ban",
    "adv": "adv",
}


async def caption_command_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اجازه می‌دهد دستور را به‌صورت کپشنِ عکس/فایل بفرستی —
    مثلاً عکس مدرک + کپشن «/scam کلاهبرداری ۵۰۰ تومنی» یا «گزارش اسکم ...»
    یا عکس تبلیغ + کپشن «/adv @channel ...»."""
    msg = update.effective_message
    if not msg or not msg.caption:
        return
    caption = msg.caption.strip()

    cmd, rest = None, ""
    if caption.startswith("/"):
        head = caption.split()[0].lstrip("/").split("@")[0].lower()
        cmd = CAPTION_COMMANDS.get(head)
        rest = caption[len(caption.split()[0]):].strip()
    else:
        norm = _normalize_command_text(caption).lower()
        words = norm.split(" ")
        for c, alias, wc in _ALIAS_LOOKUP:
            if c not in CAPTION_COMMANDS or wc > len(words):
                continue
            if " ".join(words[:wc]) == alias:
                cmd = c
                rest = " ".join(caption.split()[wc:])
                break
    if not cmd:
        return

    handler = SIMPLE_COMMANDS.get(cmd)
    if handler is None:
        return
    if cmd in ADMIN_ONLY_TEXT_COMMANDS and update.effective_chat.type in GROUP_TYPES:
        if not await is_admin(update, context):
            return
    context.args = rest.split() if rest else []
    try:
        await handler(update, context)
    except Exception as e:
        logger.exception(f"خطا در دستور کپشنی «{cmd}»: {e}")


async def chat_member_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """وقتی ادمین‌های گروه عوض می‌شوند، کش را باطل کن."""
    if update.effective_chat:
        invalidate_admin_cache(update.effective_chat.id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, ApplicationHandlerStop):
        return
    logger.exception("خطای پردازش‌نشده:", exc_info=context.error)


# ---------------------------------------------------------------------------
# راه‌اندازی
# ---------------------------------------------------------------------------
def build_application() -> Application:
    builder = Application.builder().token(BOT_TOKEN)
    if PROXY_URL:
        builder = (builder
                   .request(HTTPXRequest(proxy=PROXY_URL,
                                         connect_timeout=20, read_timeout=30))
                   .get_updates_request(HTTPXRequest(proxy=PROXY_URL,
                                                     connect_timeout=20, read_timeout=60)))
        logger.info(f"بات از طریق پروکسی اجرا می‌شود: {PROXY_URL}")
    return builder.build()


SIMPLE_COMMANDS = {
    "start": start_cmd, "menu": menu_cmd, "help": help_cmd,
    "myid": my_id, "id": my_id, "admins": admins_cmd,
    "addadmin": addadmin_cmd, "removeadmin": removeadmin_cmd,
    "groups": groups_cmd, "emojis": emojis_cmd, "emojiid": emojiid_cmd,
    "backup": backup_cmd,
    "startlottery": start_lottery, "stoplottery": stop_lottery,
    "pickwinner": pick_winner, "cancellottery": cancel_lottery_cmd,
    "setrange": setrange_cmd,
    "status": status_cmd, "myentry": my_entry, "export": export_cmd,
    "history": history_cmd, "leaderboard": leaderboard_cmd,
    "check": check_cmd, "prices": prices_cmd, "price": prices_cmd,
    "stats": stats_cmd, "mystats": my_stats_cmd, "my_stats": my_stats_cmd,
    "topactive": top_active_cmd, "top": top_active_cmd, "profile": profile_cmd,
    "info": info_cmd, "whois": info_cmd,
    "rules": rules_cmd, "setrules": set_rules_cmd, "setwelcome": set_welcome_cmd,
    "warn": warn_cmd, "unwarn": unwarn_cmd, "warnings": warnings_cmd,
    "resetwarns": resetwarns_cmd, "mute": mute_cmd, "unmute": unmute_cmd,
    "muted": muted_list_cmd,
    "kick": kick_cmd, "ban": ban_cmd, "unban": unban_cmd,
    "del": del_cmd, "delete": del_cmd, "purge": purge_cmd,
    "pin": pin_cmd, "unpin": unpin_cmd,
    "whitelist_add": whitelist_add_cmd, "whitelist_remove": whitelist_remove_cmd,
    "whitelist": whitelist_cmd,
    "mgmtsettings": mgmt_settings_cmd, "toggle": toggle_cmd,
    "setmaxwarn": setmaxwarn_cmd, "setflood": setflood_cmd,
    "setnight": setnight_cmd, "floodaction": floodaction_cmd,
    "antispamon": antispam_on_cmd, "antispamoff": antispam_off_cmd,
    "antilinkon": antilink_on_cmd, "antilinkoff": antilink_off_cmd,
    "antispamstatus": antispam_status_cmd, "antispam": antispam_status_cmd,
    # --- سامانه‌ی اسکم / تبر ---
    "report": report_cmd, "cancel": cancel_cmd,
    "scam": scam_cmd, "unscam": unscam_cmd,
    "scamcheck": scamcheck_cmd, "check_scam": scamcheck_cmd,
    "scamlist": scamlist_cmd, "blacklist": scamlist_cmd,
    "reports": reports_cmd, "report_show": report_show_cmd,
    # --- تبلیغات کانال [قابلیت جدید] ---
    "adv": adv_cmd, "ad": adv_cmd,
}

# نگاشت دستورهای متنی فارسی به همان هندلرها
COMMAND_HANDLERS_MAP.update({
    key: SIMPLE_COMMANDS[key] for key in TEXT_COMMAND_ALIASES if key in SIMPLE_COMMANDS
})


async def _post_init(app: Application):
    """منوی دستورهای تلگرام را ست می‌کند."""
    try:
        from telegram import BotCommand
        await app.bot.set_my_commands([
            BotCommand("menu", "پنل مدیریت"),
            BotCommand("help", "راهنما"),
            BotCommand("status", "وضعیت قرعه‌کشی"),
            BotCommand("myentry", "عدد ثبت‌شده‌ی من"),
            BotCommand("mystats", "آمار و لول من"),
            BotCommand("topactive", "فعال‌ترین اعضا"),
            BotCommand("rules", "قوانین گروه"),
            BotCommand("prices", "قیمت دلار و طلا"),
            BotCommand("check", "چک آی‌پی"),
            BotCommand("antispam", "وضعیت ضد اسپم"),
            BotCommand("report", "گزارش اسکم / تبر"),
            BotCommand("scamcheck", "استعلام سابقه‌ی یک نفر"),
            BotCommand("scamlist", "لیست سیاه اسکمرها"),
            BotCommand("adv", "ساخت پست تبلیغاتی برای کانال"),
        ])
    except Exception as e:
        logger.warning(f"ست کردن منوی دستورها ناموفق: {e}")

    me = await app.bot.get_me()
    logger.info(f"بات آماده است: @{me.username} ({me.id})")
    if getattr(me, "can_read_all_group_messages", None) is False:
        logger.warning(
            "⚠️ Privacy Mode روشن است! بات پیام‌های عادی گروه را نمی‌بیند و "
            "ضد اسپم/XP کار نمی‌کند. در BotFather: /setprivacy → Disable، "
            "سپس بات را از گروه خارج و دوباره اضافه کن."
        )


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN تنظیم نشده.\n"
            "توکن را از BotFather بگیر و در فایل .env (کنار bot.py) این‌طور بگذار:\n"
            "    BOT_TOKEN=123456:ABC...\n"
            f"مسیر مورد انتظار: {_ENV_PATH}\n"
            "یا اگر .env نمی‌خواهی، با export ست کن:\n"
            "    export BOT_TOKEN='123456:ABC...'\n"
            "اگر قبلاً توکن را داخل کد گذاشته بودی، حتماً با /revoke باطلش کن."
        )
    if not OWNER_IDS:
        logger.warning(
            "OWNER_IDS تنظیم نشده! فعلاً فقط ادمین‌های واقعیِ هر گروه دسترسی مدیریتی دارند. "
            "با /myid آیدی خودت را بگیر و در فایل .env این‌طور بگذار: OWNER_IDS=123456789"
        )

    init_db()
    _ensure_emoji_config_exists()
    app = build_application()
    app.post_init = _post_init

    for name, handler in SIMPLE_COMMANDS.items():
        app.add_handler(CommandHandler(name, handler))

    app.add_handler(CallbackQueryHandler(button_handler))

    # ---- ترتیب هندلرها ----
    # group=-3 : جریان ثبت گزارش اسکم در پیوی (بالاترین اولویت)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            report_flow_handler,
        ),
        group=-3,
    )

    # group=-2 : مدیریت گروه (سایلنت، شب، ضد لینک/فوروارد، ضد اسپم) روی هر نوع پیام
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL & ~filters.COMMAND,
            moderation_handler,
        ),
        group=-2,
    )

    # group=-1 : دستورهای فارسی بدون / و دستورهای کپشنی (عکس مدرک + کپشن)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_command_router),
        group=-1,
    )
    app.add_handler(MessageHandler(filters.CAPTION, caption_command_router), group=-1)

    # group=0 : تریگرها
    app.add_handler(
        MessageHandler(filters.Regex(r"(?i)^\s*check\s*!\s*\S+"), handle_check_trigger),
        group=0,
    )
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member), group=0
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_number_message), group=0
    )

    # group=1 : آمار و XP (همه‌ی انواع پیام)
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL & ~filters.COMMAND,
            xp_handler,
        ),
        group=1,
    )

    # group=2/3 : شناسایی گروه‌ها و باطل کردن کش ادمین
    app.add_handler(MessageHandler(filters.ALL, track_chat_handler), group=2)
    app.add_handler(
        MessageHandler(filters.StatusUpdate.ALL, chat_member_update_handler), group=3
    )

    app.add_error_handler(on_error)

    if app.job_queue:
        app.job_queue.run_repeating(
            lambda ctx: cleanup_old_message_logs(3600), interval=600, first=60,
        )
        app.job_queue.run_repeating(captcha_expiry_job, interval=60, first=30)
    else:
        logger.warning("job_queue فعال نیست — "
                       "با pip install \"python-telegram-bot[job-queue]\" نصبش کن.")

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
