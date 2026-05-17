"""
Telegram Bot - IP Geolocation + Refer System
Compatible with Python 3.8+
"""

import logging
import sqlite3
import hashlib
import re
import requests
from functools import wraps
from typing import Optional, List

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────── CONFIG ────────────────────────────
BOT_TOKEN   = "8887374895:AAGPDAmFEWRfrw8dOq3IbJ2c9DJDM49YqTQ"
ADMIN_ID    = 8210146346
CHANNEL     = "@saniedit9"
FREE_LIMIT  = 4
REFER_BONUS = 1
DB_FILE     = "bot_data.db"
# ───────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            full_name   TEXT,
            limits      INTEGER NOT NULL DEFAULT 4,
            referred_by INTEGER,
            refer_code  TEXT UNIQUE NOT NULL,
            is_banned   INTEGER NOT NULL DEFAULT 0,
            joined_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS refer_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            new_user_id INTEGER NOT NULL UNIQUE,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialised.")


# ══════════════════════════════════════════════════════════════
#  USER HELPERS
# ══════════════════════════════════════════════════════════════

def generate_refer_code(user_id: int) -> str:
    raw = "ref_{}_secret".format(user_id)
    return hashlib.md5(raw.encode()).hexdigest()[:10]


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    conn = get_db()
    row  = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def register_user(user_id: int, username: str, full_name: str,
                  referred_by: Optional[int] = None) -> None:
    code = generate_refer_code(user_id)
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, full_name, limits, referred_by, refer_code) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, username, full_name, FREE_LIMIT, referred_by, code),
    )
    conn.commit()
    conn.close()


def deduct_limit(user_id: int) -> bool:
    conn = get_db()
    row  = conn.execute("SELECT limits FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row or row["limits"] <= 0:
        conn.close()
        return False
    conn.execute("UPDATE users SET limits = limits - 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True


def add_limit(user_id: int, amount: int = 1) -> None:
    conn = get_db()
    conn.execute("UPDATE users SET limits = limits + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()


def set_limit(user_id: int, amount: int) -> None:
    conn = get_db()
    conn.execute("UPDATE users SET limits = ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()


def get_refer_code(user_id: int) -> Optional[str]:
    conn = get_db()
    row  = conn.execute("SELECT refer_code FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row["refer_code"] if row else None


def get_refer_count(user_id: int) -> int:
    conn  = get_db()
    row   = conn.execute(
        "SELECT COUNT(*) as c FROM refer_log WHERE referrer_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


def record_refer(referrer_id: int, new_user_id: int) -> bool:
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO refer_log (referrer_id, new_user_id) VALUES (?, ?)",
            (referrer_id, new_user_id),
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False


def ban_user(user_id: int) -> None:
    conn = get_db()
    conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def unban_user(user_id: int) -> None:
    conn = get_db()
    conn.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_all_users() -> List[sqlite3.Row]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM users").fetchall()
    conn.close()
    return rows


def get_total_users() -> int:
    conn  = get_db()
    row   = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
    conn.close()
    return row["c"] if row else 0


def get_user_by_refer_code(code: str) -> Optional[sqlite3.Row]:
    conn = get_db()
    row  = conn.execute("SELECT * FROM users WHERE refer_code = ?", (code,)).fetchone()
    conn.close()
    return row


# ══════════════════════════════════════════════════════════════
#  GEOLOCATION
# ══════════════════════════════════════════════════════════════

def lookup_ip(ip: str) -> Optional[dict]:
    try:
        url  = (
            "http://ip-api.com/json/{}?fields="
            "status,message,country,regionName,city,zip,lat,lon,timezone,isp,org,query".format(ip)
        )
        resp = requests.get(url, timeout=8)
        data = resp.json()
        if data.get("status") == "success":
            return data
        return None
    except Exception as exc:
        logger.error("IP lookup error: %s", exc)
        return None


def is_valid_ip(text: str) -> bool:
    ipv4   = r"^(\d{1,3}\.){3}\d{1,3}$"
    domain = r"^[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(ipv4, text) or re.match(domain, text))


# ══════════════════════════════════════════════════════════════
#  CHANNEL MEMBERSHIP CHECK
# ══════════════════════════════════════════════════════════════

async def is_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🌐 IP to Location"), KeyboardButton("🔗 My Refer Link")],
        [KeyboardButton("💰 My Balance"),     KeyboardButton("⚙️ Admin Panel")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url="https://t.me/{}".format(CHANNEL.lstrip("@")))],
        [InlineKeyboardButton("✅ I Joined",      callback_data="check_join")],
    ])


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Bot Stats",           callback_data="admin_stats")],
        [InlineKeyboardButton("📢 Broadcast Message",   callback_data="admin_broadcast")],
        [InlineKeyboardButton("➕ Add Limits (User)",    callback_data="admin_add_limit")],
        [InlineKeyboardButton("➖ Remove Limits (User)", callback_data="admin_remove_limit")],
        [InlineKeyboardButton("🚫 Ban User",             callback_data="admin_ban")],
        [InlineKeyboardButton("✅ Unban User",            callback_data="admin_unban")],
        [InlineKeyboardButton("🔍 User Info",            callback_data="admin_user_info")],
    ])


# ══════════════════════════════════════════════════════════════
#  GUARDS / DECORATORS
# ══════════════════════════════════════════════════════════════

def require_membership(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not await is_member(ctx.bot, user.id):
            await update.message.reply_text(
                "⛔ *You must join our channel first!*\n\n"
                "Please join the channel then click ✅ I Joined.",
                parse_mode="Markdown",
                reply_markup=join_keyboard(),
            )
            return
        return await func(update, ctx)
    return wrapper


def require_not_banned(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        row     = get_user(user_id)
        if row and row["is_banned"]:
            await update.message.reply_text("🚫 You have been banned from using this bot.")
            return
        return await func(update, ctx)
    return wrapper


# ══════════════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user      = update.effective_user
    user_id   = user.id
    username  = user.username or ""
    full_name = user.full_name or ""

    referrer_id = None
    if ctx.args:
        code     = ctx.args[0]
        referrer = get_user_by_refer_code(code)
        if referrer and referrer["user_id"] != user_id:
            referrer_id = referrer["user_id"]

    register_user(user_id, username, full_name, referred_by=referrer_id)

    row = get_user(user_id)

    # Credit referrer only once
    if referrer_id and row and record_refer(referrer_id, user_id):
        add_limit(referrer_id, REFER_BONUS)
        try:
            await ctx.bot.send_message(
                referrer_id,
                "🎉 Someone joined using your refer link!\n"
                "✅ +{} limit added to your account.".format(REFER_BONUS),
            )
        except Exception:
            pass

    if not await is_member(ctx.bot, user_id):
        await update.message.reply_text(
            "👋 *Welcome!*\n\nTo use this bot you must join our channel first.",
            parse_mode="Markdown",
            reply_markup=join_keyboard(),
        )
        return

    lim = row["limits"] if row else FREE_LIMIT
    await update.message.reply_text(
        "👋 *Welcome, {}!*\n\n"
        "🔍 Look up any IP address location\n"
        "🎁 Refer friends to earn extra limits\n\n"
        "You have *{}* lookups remaining.\n\n"
        "Choose an option below:".format(full_name, lim),
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


# ══════════════════════════════════════════════════════════════
#  BUTTON HANDLERS
# ══════════════════════════════════════════════════════════════

async def handle_ip_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["awaiting_ip"] = True
    await update.message.reply_text(
        "🌐 *IP to Location*\n\n"
        "Send the IP address you want to look up.\n"
        "Example: `8.8.8.8`\n\n"
        "Each lookup costs *1 limit*.",
        parse_mode="Markdown",
    )


async def process_ip_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text    = update.message.text.strip()

    if not is_valid_ip(text):
        await update.message.reply_text(
            "❌ Invalid format. Please send a valid IP.\nExample: `8.8.8.8`",
            parse_mode="Markdown",
        )
        return

    row = get_user(user_id)
    if not row or row["limits"] <= 0:
        await update.message.reply_text(
            "⚠️ *No lookups remaining!*\n\n"
            "🔗 Share your refer link to earn more limits.\n"
            "Each successful referral gives +1 limit.",
            parse_mode="Markdown",
        )
        return

    if not deduct_limit(user_id):
        await update.message.reply_text("⚠️ No limits left. Refer friends to earn more!")
        return

    await update.message.reply_text("🔍 Looking up, please wait...")

    data = lookup_ip(text)
    row  = get_user(user_id)

    if not data:
        add_limit(user_id, 1)  # refund on failure
        await update.message.reply_text(
            "❌ Could not retrieve location for that IP.\nYour limit has been refunded."
        )
        return

    remaining = row["limits"] if row else 0

    result = (
        "📍 *IP Location Result*\n"
        "──────────────────────────────\n"
        "🔢 *IP:*        `{}`\n"
        "🌍 *Country:*   {}\n"
        "🏙️ *Region:*    {}\n"
        "🏘️ *City:*      {}\n"
        "📮 *ZIP:*       {}\n"
        "📡 *Timezone:*  {}\n"
        "📶 *ISP:*       {}\n"
        "🏢 *Org:*       {}\n"
        "🗺️ *Lat/Lon:*   {}, {}\n"
        "──────────────────────────────\n"
        "💰 *Remaining Limits:* {}"
    ).format(
        data.get("query", text),
        data.get("country", "N/A"),
        data.get("regionName", "N/A"),
        data.get("city", "N/A"),
        data.get("zip", "N/A"),
        data.get("timezone", "N/A"),
        data.get("isp", "N/A"),
        data.get("org", "N/A"),
        data.get("lat", "N/A"),
        data.get("lon", "N/A"),
        remaining,
    )

    google_url = "https://www.google.com/maps?q={},{}".format(
        data.get("lat", ""), data.get("lon", "")
    )
    ctx.user_data["awaiting_ip"] = False
    await update.message.reply_text(
        result,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗺️ View on Google Maps", url=google_url)]
        ]),
    )


async def handle_refer_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id  = update.effective_user.id
    code     = get_refer_code(user_id)
    count    = get_refer_count(user_id)
    bot_info = await ctx.bot.get_me()
    link     = "https://t.me/{}?start={}".format(bot_info.username, code)

    await update.message.reply_text(
        "🔗 *Your Refer Link*\n\n"
        "`{}`\n\n"
        "👥 *Total Referrals:* {}\n"
        "🎁 *Bonus per Refer:* +{} limit\n\n"
        "Share this link with friends.\n"
        "When they join, you earn a bonus limit automatically!".format(link, count, REFER_BONUS),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "📤 Share Link",
                url="https://t.me/share/url?url={}&text=Join+and+get+free+IP+lookups!".format(link)
            )]
        ]),
    )


async def handle_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    row     = get_user(user_id)
    count   = get_refer_count(user_id)

    if not row:
        await update.message.reply_text("❌ User not found. Please send /start first.")
        return

    await update.message.reply_text(
        "💰 *Your Account Balance*\n"
        "──────────────────────────────\n"
        "🔍 *Remaining Lookups:* {}\n"
        "👥 *Total Referrals:*   {}\n"
        "📅 *Joined:*            {}\n"
        "──────────────────────────────\n\n"
        "🔗 Refer more friends to earn more limits!".format(
            row["limits"], count, row["joined_at"][:10]
        ),
        parse_mode="Markdown",
    )


async def handle_admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 You are not authorised to access the admin panel.")
        return
    await update.message.reply_text(
        "⚙️ *Admin Panel*\n\nChoose an action:",
        parse_mode="Markdown",
        reply_markup=admin_panel_keyboard(),
    )


# ══════════════════════════════════════════════════════════════
#  CALLBACK QUERY HANDLER
# ══════════════════════════════════════════════════════════════

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    user_id = query.from_user.id
    data    = query.data
    await query.answer()

    # ── Channel join verification ───────────────────────────
    if data == "check_join":
        if await is_member(ctx.bot, user_id):
            row = get_user(user_id)
            lim = row["limits"] if row else FREE_LIMIT
            await query.edit_message_text(
                "✅ *Verified!* Welcome aboard!\n\n"
                "You have *{}* free lookups.\n"
                "Use the buttons below.".format(lim),
                parse_mode="Markdown",
            )
            await ctx.bot.send_message(user_id, "Choose an option:", reply_markup=MAIN_KEYBOARD)
        else:
            await query.answer("❌ You haven't joined the channel yet!", show_alert=True)
        return

    # ── Admin-only callbacks ────────────────────────────────
    if user_id != ADMIN_ID:
        await query.answer("🚫 Access denied.", show_alert=True)
        return

    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]])

    if data == "admin_stats":
        total  = get_total_users()
        users  = get_all_users()
        banned = sum(1 for u in users if u["is_banned"])
        refs   = sum(get_refer_count(u["user_id"]) for u in users)
        await query.edit_message_text(
            "📊 *Bot Statistics*\n"
            "──────────────────────────────\n"
            "👤 Total Users:  {}\n"
            "🚫 Banned:       {}\n"
            "🔗 Total Refers: {}".format(total, banned, refs),
            parse_mode="Markdown",
            reply_markup=back_btn,
        )

    elif data == "admin_broadcast":
        ctx.user_data["admin_action"] = "broadcast"
        await query.edit_message_text(
            "📢 *Broadcast*\n\nSend the message to broadcast to all users.",
            parse_mode="Markdown",
        )

    elif data == "admin_add_limit":
        ctx.user_data["admin_action"] = "add_limit"
        await query.edit_message_text(
            "➕ *Add Limits*\n\nReply with:\n`<user_id> <amount>`\nExample: `123456789 5`",
            parse_mode="Markdown",
        )

    elif data == "admin_remove_limit":
        ctx.user_data["admin_action"] = "remove_limit"
        await query.edit_message_text(
            "➖ *Remove Limits*\n\nReply with:\n`<user_id> <amount>`\nExample: `123456789 2`",
            parse_mode="Markdown",
        )

    elif data == "admin_ban":
        ctx.user_data["admin_action"] = "ban"
        await query.edit_message_text(
            "🚫 *Ban User*\n\nSend the user ID:\nExample: `123456789`",
            parse_mode="Markdown",
        )

    elif data == "admin_unban":
        ctx.user_data["admin_action"] = "unban"
        await query.edit_message_text(
            "✅ *Unban User*\n\nSend the user ID:\nExample: `123456789`",
            parse_mode="Markdown",
        )

    elif data == "admin_user_info":
        ctx.user_data["admin_action"] = "user_info"
        await query.edit_message_text(
            "🔍 *User Info*\n\nSend the user ID:\nExample: `123456789`",
            parse_mode="Markdown",
        )

    elif data == "admin_back":
        await query.edit_message_text(
            "⚙️ *Admin Panel*\n\nChoose an action:",
            parse_mode="Markdown",
            reply_markup=admin_panel_keyboard(),
        )


# ══════════════════════════════════════════════════════════════
#  MAIN TEXT MESSAGE ROUTER
# ══════════════════════════════════════════════════════════════

@require_not_banned
@require_membership
async def message_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text    = update.message.text.strip()
    user_id = update.effective_user.id

    # ── Keyboard buttons ────────────────────────────────────
    if text == "🌐 IP to Location":
        await handle_ip_location(update, ctx)
        return

    if text == "🔗 My Refer Link":
        await handle_refer_link(update, ctx)
        return

    if text == "💰 My Balance":
        await handle_balance(update, ctx)
        return

    if text == "⚙️ Admin Panel":
        await handle_admin_panel(update, ctx)
        return

    # ── Admin action input ──────────────────────────────────
    if user_id == ADMIN_ID:
        action = ctx.user_data.get("admin_action")

        if action == "broadcast":
            ctx.user_data["admin_action"] = None
            users     = get_all_users()
            sent_ok   = 0
            sent_fail = 0
            for u in users:
                if u["is_banned"]:
                    continue
                try:
                    await ctx.bot.send_message(u["user_id"], text)
                    sent_ok += 1
                except Exception:
                    sent_fail += 1
            await update.message.reply_text(
                "📢 Broadcast done!\n✅ Sent: {}\n❌ Failed: {}".format(sent_ok, sent_fail),
                reply_markup=MAIN_KEYBOARD,
            )
            return

        if action in ("add_limit", "remove_limit"):
            ctx.user_data["admin_action"] = None
            parts = text.split()
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                await update.message.reply_text(
                    "❌ Invalid format. Use: `<user_id> <amount>`",
                    parse_mode="Markdown",
                )
                return
            target_id = int(parts[0])
            amount    = int(parts[1])
            target    = get_user(target_id)
            if not target:
                await update.message.reply_text("❌ User not found.")
                return
            if action == "add_limit":
                add_limit(target_id, amount)
                await update.message.reply_text(
                    "✅ Added {} limit(s) to user `{}`.".format(amount, target_id),
                    parse_mode="Markdown",
                    reply_markup=MAIN_KEYBOARD,
                )
            else:
                new_val = max(0, target["limits"] - amount)
                set_limit(target_id, new_val)
                await update.message.reply_text(
                    "✅ Removed {} limit(s) from user `{}`.\nRemaining: {}".format(
                        amount, target_id, new_val
                    ),
                    parse_mode="Markdown",
                    reply_markup=MAIN_KEYBOARD,
                )
            return

        if action == "ban":
            ctx.user_data["admin_action"] = None
            if not text.isdigit():
                await update.message.reply_text("❌ Send a valid numeric user ID.")
                return
            ban_user(int(text))
            await update.message.reply_text(
                "🚫 User `{}` has been banned.".format(text),
                parse_mode="Markdown",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        if action == "unban":
            ctx.user_data["admin_action"] = None
            if not text.isdigit():
                await update.message.reply_text("❌ Send a valid numeric user ID.")
                return
            unban_user(int(text))
            await update.message.reply_text(
                "✅ User `{}` has been unbanned.".format(text),
                parse_mode="Markdown",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        if action == "user_info":
            ctx.user_data["admin_action"] = None
            if not text.isdigit():
                await update.message.reply_text("❌ Send a valid numeric user ID.")
                return
            u = get_user(int(text))
            if not u:
                await update.message.reply_text("❌ User not found.")
                return
            refs = get_refer_count(u["user_id"])
            await update.message.reply_text(
                "🔍 *User Info*\n"
                "──────────────────────────────\n"
                "🆔 ID:       `{}`\n"
                "👤 Name:     {}\n"
                "📛 Username: @{}\n"
                "💰 Limits:   {}\n"
                "👥 Refers:   {}\n"
                "🚫 Banned:   {}\n"
                "📅 Joined:   {}".format(
                    u["user_id"],
                    u["full_name"],
                    u["username"] or "N/A",
                    u["limits"],
                    refs,
                    "Yes" if u["is_banned"] else "No",
                    u["joined_at"][:10],
                ),
                parse_mode="Markdown",
                reply_markup=MAIN_KEYBOARD,
            )
            return

    # ── Awaiting IP ─────────────────────────────────────────
    if ctx.user_data.get("awaiting_ip"):
        await process_ip_input(update, ctx)
        return

    # ── Fallback ─────────────────────────────────────────────
    await update.message.reply_text("ℹ️ Please use the buttons below.", reply_markup=MAIN_KEYBOARD)


# ══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════

def main() -> None:
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))
    logger.info("Bot is running...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
