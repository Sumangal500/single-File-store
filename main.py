
import os
import re
import html
import sqlite3
import secrets
import logging
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    filters,
)

# ================= CONFIG =================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DB = BASE_DIR / "blackshield.db"
MAX_CHANNELS = 6
PAGE_SIZE = 8

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BLACKSHIELD")

CHANNEL_NAMES = [
    "CHANNEL ONE", "CHANNEL TWO", "CHANNEL THREE",
    "CHANNEL FOUR", "CHANNEL FIVE", "CHANNEL SIX"
]


# ================= DATABASE =================

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def columns(con, table):
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS channels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT UNIQUE,
            title TEXT,
            invite_link TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        con.execute("""
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE,
            file_id TEXT,
            file_type TEXT,
            file_name TEXT,
            caption TEXT,
            credit TEXT,
            button_text TEXT,
            button_url TEXT,
            owner INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        con.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            first_name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        con.execute("""
        CREATE TABLE IF NOT EXISTS deliveries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            token TEXT,
            delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        con.execute("""
        CREATE TABLE IF NOT EXISTS pending_requests(
            user_id TEXT,
            chat_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, chat_id)
        )""")

        con.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )""")

        if "join_mode" not in columns(con, "channels"):
            con.execute(
                "ALTER TABLE channels ADD COLUMN join_mode TEXT DEFAULT 'normal'"
            )

        if "content_text" not in columns(con, "files"):
            con.execute(
                "ALTER TABLE files ADD COLUMN content_text TEXT DEFAULT ''"
            )


def is_admin(uid):
    return uid in ADMINS


def esc(value):
    return html.escape(str(value or ""))


def valid_url(value):
    try:
        p = urlparse(value)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def get_channels():
    with db() as con:
        return con.execute("SELECT * FROM channels ORDER BY id").fetchall()


def get_channel_label(channel_id):
    for i, ch in enumerate(get_channels()):
        if ch["id"] == channel_id:
            return CHANNEL_NAMES[i]
    return "CHANNEL"


def get_file(token):
    with db() as con:
        return con.execute(
            "SELECT * FROM files WHERE token=?", (token,)
        ).fetchone()


def remember_user(user):
    with db() as con:
        con.execute("""
        INSERT INTO users(user_id, first_name, username)
        VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
        first_name=excluded.first_name,
        username=excluded.username,
        last_seen=CURRENT_TIMESTAMP
        """, (user.id, user.first_name or "", user.username or ""))


def save_pending(uid, chat_id):
    with db() as con:
        con.execute("""
        INSERT OR REPLACE INTO pending_requests(user_id, chat_id)
        VALUES(?,?)
        """, (str(uid), str(chat_id)))


def pending(uid, chat_id):
    with db() as con:
        return con.execute("""
        SELECT 1 FROM pending_requests
        WHERE user_id=? AND chat_id=?
        """, (str(uid), str(chat_id))).fetchone() is not None


def clear_pending(uid, chat_id):
    with db() as con:
        con.execute("""
        DELETE FROM pending_requests WHERE user_id=? AND chat_id=?
        """, (str(uid), str(chat_id)))


def save_delivery(uid, token):
    with db() as con:
        con.execute(
            "INSERT INTO deliveries(user_id,token) VALUES(?,?)",
            (uid, token)
        )


# ================= KEYBOARDS =================

def user_keyboard(uid):
    rows = [
        [
            InlineKeyboardButton("📁 MY FILES", callback_data="myfiles"),
            InlineKeyboardButton("🕘 RECENT FILES", callback_data="recent")
        ],
        [
            InlineKeyboardButton("🔎 SEARCH MY FILES", callback_data="search"),
            InlineKeyboardButton("🆘 HELP & SUPPORT", callback_data="support")
        ]
    ]
    if is_admin(uid):
        rows.append([
            InlineKeyboardButton("⚙️ ADMIN PANEL", callback_data="admin")
        ])
    return InlineKeyboardMarkup(rows)


def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ ADD CHANNEL", callback_data="addchannel"),
            InlineKeyboardButton("📢 MY CHANNELS", callback_data="channels")
        ],
        [InlineKeyboardButton("🔗 GET LINK", callback_data="getlink")],
        [
            InlineKeyboardButton("📣 BROADCAST", callback_data="broadcast"),
            InlineKeyboardButton("📊 STATISTICS", callback_data="stats")
        ],
        [InlineKeyboardButton("🏠 USER MENU", callback_data="home")]
    ])


def back_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ BACK", callback_data="admin")]
    ])


# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    remember_user(user)

    if context.args and context.args[0].startswith("file_"):
        await deliver_file(
            update, context, context.args[0][5:]
        )
        return

    await update.effective_message.reply_text(
        f"🛡️ BLACKSHIELD FILE SYSTEM\n\n"
        f"Welcome {esc(user.first_name)}!\n\n"
        "Choose an option:",
        parse_mode=ParseMode.HTML,
        reply_markup=user_keyboard(user.id)
    )


# ================= MEMBERSHIP =================

async def membership_status(context, uid, channel):
    cid = channel["chat_id"]

    try:
        member = await context.bot.get_chat_member(
            chat_id=int(cid),
            user_id=uid
        )

        if member.status in ("creator", "administrator", "member"):
            clear_pending(uid, cid)
            return "joined"

        if member.status == "restricted" and getattr(member, "is_member", False):
            clear_pending(uid, cid)
            return "joined"

        if pending(uid, cid):
            return "pending"

        return "not_joined"

    except Exception:
        log.exception("Membership check failed")
        if pending(uid, cid):
            return "pending"
        return "unknown"


def membership_markup(token, missing):
    rows = []

    for ch, status in missing:
        label = get_channel_label(ch["id"])

        if status == "pending":
            rows.append([
                InlineKeyboardButton(
                    f"⏳ {label} · REQUEST PENDING",
                    callback_data="pending_info"
                )
            ])

        elif status == "unknown":
            rows.append([
                InlineKeyboardButton(
                    f"⚠️ {label} · CHECK AGAIN",
                    callback_data=f"verify:{token}"
                )
            ])

        elif ch["invite_link"]:
            rows.append([
                InlineKeyboardButton(
                    f"📢 JOIN {label}",
                    url=ch["invite_link"]
                )
            ])

    rows.append([
        InlineKeyboardButton(
            "✅ I'VE JOINED · VERIFY",
            callback_data=f"verify:{token}"
        )
    ])

    return InlineKeyboardMarkup(rows)


async def show_membership(update, token, missing):
    lines = [
        "🔐 CHANNEL VERIFICATION",
        "",
        "Complete the channel requirements below.",
        "The file will be delivered after verification.",
        ""
    ]

    for ch, status in missing:
        label = get_channel_label(ch["id"])
        if status == "pending":
            lines.append(f"⏳ {label}: REQUEST PENDING")
        elif status == "unknown":
            lines.append(f"⚠️ {label}: CHECK UNAVAILABLE")
        else:
            lines.append(f"❌ {label}: NOT JOINED")

    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=membership_markup(token, missing)
    )


# ================= DELIVERY =================

async def send_item(context, chat_id, item):
    name = esc(item["file_name"] or "Shared Item")
    credit = esc(item["credit"] or "BLACKSHIELD")

    caption = (
        "✨ <b>PREMIUM FILE</b>\n\n"
        f"📁 <b>Name:</b> {name}\n"
        f"✍️ <b>Credit:</b> {credit}\n\n"
        "🔐 Secure Private Access"
    )

    button = None
    if item["file_type"] == "url":
        url = item["content_text"] or item["button_url"]
        if valid_url(url):
            button = InlineKeyboardMarkup([[
                InlineKeyboardButton("🌐 OPEN WEBSITE", url=url)
            ]])
    elif item["button_url"] and valid_url(item["button_url"]):
        button = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                item["button_text"] or "🔗 OPEN LINK",
                url=item["button_url"]
            )
        ]])

    common = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": ParseMode.HTML,
        "reply_markup": button
    }

    kind = item["file_type"]

    if kind == "document":
        await context.bot.send_document(document=item["file_id"], **common)
    elif kind == "video":
        await context.bot.send_video(video=item["file_id"], **common)
    elif kind == "photo":
        await context.bot.send_photo(photo=item["file_id"], **common)
    elif kind == "audio":
        await context.bot.send_audio(audio=item["file_id"], **common)
    elif kind == "animation":
        await context.bot.send_animation(animation=item["file_id"], **common)
    elif kind == "voice":
        await context.bot.send_voice(voice=item["file_id"], **common)
    elif kind == "message":
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption + "\n\n" + esc(item["content_text"]),
            parse_mode=ParseMode.HTML,
            reply_markup=button
        )
    elif kind == "url":
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption + "\n\nTap below to open the website.",
            parse_mode=ParseMode.HTML,
            reply_markup=button
        )
    else:
        raise ValueError("Unsupported item type")


async def deliver_file(update, context, token):
    item = get_file(token)

    if not item:
        await update.effective_message.reply_text(
            "❌ Invalid or expired file link."
        )
        return

    user = update.effective_user
    remember_user(user)

    missing = []

    # Always recheck every configured channel.
    for ch in get_channels():
        status = await membership_status(context, user.id, ch)
        if status != "joined":
            missing.append((ch, status))

    if missing:
        await show_membership(update, token, missing)
        return

    try:
        await send_item(
            context,
            update.effective_chat.id,
            item
        )
        save_delivery(user.id, token)
    except Exception:
        log.exception("Delivery failed")
        await update.effective_message.reply_text(
            "❌ Delivery failed. Please try again."
        )


async def verify_callback(update, context):
    q = update.callback_query
    token = q.data.split(":", 1)[1]
    item = get_file(token)

    await q.answer("Checking membership...")

    if not item:
        await q.message.reply_text("❌ File not found.")
        return

    missing = []
    for ch in get_channels():
        status = await membership_status(
            context, q.from_user.id, ch
        )
        if status != "joined":
            missing.append((ch, status))

    if missing:
        await q.message.reply_text(
            "🔐 Membership is not complete yet.",
            reply_markup=membership_markup(token, missing)
        )
        return

    try:
        await send_item(context, q.message.chat_id, item)
        save_delivery(q.from_user.id, token)
    except Exception:
        log.exception("Verified delivery failed")
        await q.message.reply_text("❌ Could not deliver the item.")


async def join_request_handler(update, context):
    request = update.chat_join_request
    if not request:
        return

    save_pending(request.from_user.id, request.chat.id)

    # Never approve requests automatically.
    try:
        await context.bot.send_message(
            request.from_user.id,
            "⏳ Your join request was received. "
            "The channel administrators must approve it."
        )
    except Exception:
        pass


# ================= USER HISTORY =================

async def history(update, recent=False, search_text=""):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()

    with db() as con:
        if search_text:
            rows = con.execute("""
            SELECT f.token, f.file_name, MAX(d.delivered_at) AS received
            FROM deliveries d JOIN files f ON f.token=d.token
            WHERE d.user_id=? AND f.file_name LIKE ?
            GROUP BY f.token ORDER BY received DESC LIMIT ?
            """, (uid, f"%{search_text}%", PAGE_SIZE)).fetchall()
        else:
            rows = con.execute("""
            SELECT f.token, f.file_name, MAX(d.delivered_at) AS received
            FROM deliveries d JOIN files f ON f.token=d.token
            WHERE d.user_id=?
            GROUP BY f.token ORDER BY received DESC LIMIT ?
            """, (uid, PAGE_SIZE)).fetchall()

    if not rows:
        await q.message.reply_text(
            "No delivered files found.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 HOME", callback_data="home")
            ]])
        )
        return

    lines = ["📁 YOUR RECEIVED FILES\n"]
    buttons = []

    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['file_name'] or 'Shared Item'}")
        buttons.append([
            InlineKeyboardButton(
                f"📥 OPEN {i}",
                callback_data=f"history:{row['token']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("🏠 HOME", callback_data="home")
    ])

    await q.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ================= ADMIN: CHANNEL SETUP =================

async def add_channel_start(update, context):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Unauthorized", show_alert=True)
        return

    if len(get_channels()) >= MAX_CHANNELS:
        await q.answer("Maximum six channels.", show_alert=True)
        return

    context.user_data["mode"] = "add_channel"
    await q.answer()
    await q.message.reply_text(
        "➕ Send the public channel @username or t.me link.\n"
        "For a private channel, forward one of its posts.\n\n"
        "First make this bot an administrator with Invite Users permission.",
        reply_markup=back_button()
    )


async def detect_channel(message, context):
    chat = None
    value = (message.text or "").strip()

    origin = message.forward_origin
    if origin and getattr(origin, "type", None) == "channel":
        chat = origin.chat
    else:
        if value.startswith("@"):
            username = value[1:]
        else:
            p = urlparse(value)
            if p.netloc.lower() not in ("t.me", "www.t.me", "telegram.me"):
                await message.reply_text("❌ Invalid channel link.")
                return
            username = p.path.strip("/").split("/")[0]

        if username.startswith("+") or username.startswith("joinchat"):
            await message.reply_text(
                "Forward a post from the private channel instead."
            )
            return

        if not re.fullmatch(r"[A-Za-z0-9_]{5,}", username):
            await message.reply_text("❌ Invalid username.")
            return

        try:
            chat = await context.bot.get_chat("@" + username)
        except Exception:
            await message.reply_text("❌ Channel not found.")
            return

    if not chat or chat.type != "channel":
        await message.reply_text("❌ This is not a channel.")
        return

    try:
        member = await context.bot.get_chat_member(
            chat.id, context.bot.id
        )
        if member.status not in ("administrator", "creator"):
            await message.reply_text("⚠️ Bot must be channel admin.")
            return
        if not getattr(member, "can_invite_users", False):
            await message.reply_text(
                "⚠️ Enable Invite Users permission for the bot."
            )
            return
    except Exception:
        await message.reply_text("⚠️ Cannot verify bot permissions.")
        return

    context.user_data["pending_channel"] = {
        "id": str(chat.id),
        "title": chat.title or "Channel"
    }
    context.user_data["mode"] = "choose_join"

    await message.reply_text(
        f"📢 Channel detected: {esc(chat.title)}\n\nChoose join type:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 NORMAL JOIN", callback_data="mode:normal")],
            [InlineKeyboardButton("🟠 REQUEST TO JOIN", callback_data="mode:request")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="cancel_channel")]
        ])
    )


async def choose_join_mode(update, context):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Unauthorized", show_alert=True)
        return

    pending_channel = context.user_data.get("pending_channel")
    if not pending_channel:
        await q.answer("No pending channel.", show_alert=True)
        return

    mode = q.data.split(":")[1]

    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=int(pending_channel["id"]),
            creates_join_request=(mode == "request"),
            name="BLACKSHIELD"
        )
    except Exception:
        await q.answer("Could not create invite link.", show_alert=True)
        return

    context.user_data["pending_channel"]["link"] = invite.invite_link
    context.user_data["pending_channel"]["join_mode"] = mode

    await q.answer()
    await q.message.reply_text(
        f"Channel: {esc(pending_channel['title'])}\n"
        f"Mode: {mode.upper()}\n\n"
        "Confirm save?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ SAVE", callback_data="confirm_channel"),
            InlineKeyboardButton("❌ CANCEL", callback_data="cancel_channel")
        ]])
    )


async def confirm_channel(update, context):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Unauthorized", show_alert=True)
        return

    p = context.user_data.get("pending_channel")
    if not p or not p.get("link"):
        await q.answer("No channel to save.", show_alert=True)
        return

    with db() as con:
        try:
            count = con.execute(
                "SELECT COUNT(*) FROM channels"
            ).fetchone()[0]
            if count >= MAX_CHANNELS:
                await q.answer("Maximum six channels.", show_alert=True)
                return

            con.execute("""
            INSERT INTO channels(chat_id,title,invite_link,join_mode)
            VALUES(?,?,?,?)
            """, (
                p["id"], p["title"], p["link"], p["join_mode"]
            ))
        except sqlite3.IntegrityError:
            await q.answer("Channel already exists.", show_alert=True)
            return

    context.user_data.pop("pending_channel", None)
    context.user_data.pop("mode", None)
    await q.answer("Channel saved")
    await q.message.reply_text(
        "✅ Channel added successfully.",
        reply_markup=back_button()
    )


async def cancel_channel(update, context):
    q = update.callback_query
    context.user_data.pop("pending_channel", None)
    context.user_data.pop("mode", None)
    await q.answer("Cancelled")
    await q.message.reply_text("Channel setup cancelled.")


async def show_channels(update, context):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Unauthorized", show_alert=True)
        return

    channels = get_channels()
    await q.answer()

    if not channels:
        await q.message.reply_text("No channels saved.")
        return

    lines = ["📢 SAVED CHANNELS\n"]
    buttons = []

    for i, ch in enumerate(channels):
        label = CHANNEL_NAMES[i]
        lines.append(
            f"{label} · {ch['join_mode'].upper()} · ID {ch['chat_id']}"
        )
        buttons.append([
            InlineKeyboardButton(
                f"🗑 REMOVE {label}",
                callback_data=f"remove:{ch['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("⬅️ BACK", callback_data="admin")
    ])

    await q.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def remove_channel(update, context):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Unauthorized", show_alert=True)
        return

    cid = int(q.data.split(":")[1])

    with db() as con:
        con.execute("DELETE FROM channels WHERE id=?", (cid,))
        con.execute(
            "DELETE FROM pending_requests WHERE chat_id IN "
            "(SELECT chat_id FROM channels WHERE id=?)",
            (cid,)
        )

    await q.answer("Removed")
    await q.message.reply_text("✅ Channel removed.")


# ================= ADMIN: CREATE LINK =================

async def getlink_start(update, context):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Unauthorized", show_alert=True)
        return

    await q.answer()
    await q.message.reply_text(
        "Choose item type:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📁 FILE", callback_data="type:file")],
            [InlineKeyboardButton("💬 MESSAGE", callback_data="type:message")],
            [InlineKeyboardButton("🌐 URL", callback_data="type:url")],
            [InlineKeyboardButton("⬅️ BACK", callback_data="admin")]
        ])
    )


async def choose_type(update, context):
    q = update.callback_query
    kind = q.data.split(":")[1]

    context.user_data["mode"] = "save_" + kind
    await q.answer()

    prompts = {
        "file": "Send a document, photo, video, audio, animation or voice message.",
        "message": "Send the text message.",
        "url": "Send a valid HTTP or HTTPS URL."
    }

    await q.message.reply_text(prompts[kind])


async def save_content(update, context):
    m = update.effective_message
    user = update.effective_user
    mode = context.user_data.get("mode")

    if not is_admin(user.id):
        return

    kind = ""
    file_id = ""
    name = ""
    content_text = ""
    caption = m.caption or ""
    button_text = ""
    button_url = ""

    if mode == "save_message":
        if not m.text:
            await m.reply_text("Send a text message.")
            return
        kind = "message"
        name = "Shared Message"
        content_text = m.text

    elif mode == "save_url":
        url = (m.text or "").strip()
        if not valid_url(url):
            await m.reply_text("Invalid HTTP/HTTPS URL.")
            return
        kind = "url"
        name = "Shared Website"
        content_text = url
        button_text = "OPEN WEBSITE"
        button_url = url

    elif mode == "save_file":
        media = None

        if m.document:
            media = (m.document.file_id, "document", m.document.file_name or "Document")
        elif m.video:
            media = (m.video.file_id, "video", m.video.file_name or "Video")
        elif m.photo:
            media = (m.photo[-1].file_id, "photo", "Photo")
        elif m.audio:
            media = (m.audio.file_id, "audio", m.audio.file_name or "Audio")
        elif m.animation:
            media = (m.animation.file_id, "animation", "Animation")
        elif m.voice:
            media = (m.voice.file_id, "voice", "Voice")

        if not media:
            await m.reply_text("Unsupported file type.")
            return

        file_id, kind, name = media

    else:
        return

    token = secrets.token_urlsafe(18)
    credit = "@" + user.username if user.username else "BLACKSHIELD"

    with db() as con:
        con.execute("""
        INSERT INTO files(
            token,file_id,file_type,file_name,caption,credit,
            button_text,button_url,owner,content_text
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (
            token,file_id,kind,name,caption,credit,
            button_text,button_url,user.id,content_text
        ))

    context.user_data.pop("mode", None)

    bot = await context.bot.get_me()
    link = f"https://t.me/{bot.username}?start=file_{token}"

    context.user_data["last_token"] = token

    await m.reply_text(
        "✨ <b>FILE LINK CREATED</b>\n\n"
        f"📁 Name: {esc(name)}\n"
        f"✍️ Credit: {esc(credit)}\n"
        "🔐 Secure Access\n\n"
        f"<code>{esc(link)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 SHARE CHANNELS", callback_data=f"share:{token}")],
            [InlineKeyboardButton("🔗 OPEN LINK", url=link)],
            [InlineKeyboardButton("⚙️ ADMIN PANEL", callback_data="admin")]
        ])
    )


# ================= SHARE CHANNELS =================

async def share_start(update, context):
    q = update.callback_query
    token = q.data.split(":")[1]

    if not is_admin(q.from_user.id):
        await q.answer("Unauthorized", show_alert=True)
        return

    if not get_file(token):
        await q.answer("File not found.", show_alert=True)
        return

    if not get_channels():
        await q.answer("No channels saved.", show_alert=True)
        return

    context.user_data["publish_token"] = token
    context.user_data["selected_channels"] = set()

    await q.answer()
    await q.message.reply_text(
        "📢 SHARE CHANNELS\n\nChoose destination:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 ALL CHANNELS", callback_data="publish:all")],
            [InlineKeyboardButton("☑️ CHOOSE CHANNELS", callback_data="publish:choose")],
            [InlineKeyboardButton("❌ CANCEL", callback_data="publish:cancel")]
        ])
    )


async def publish_post(context, channel, item, link):
    label = get_channel_label(channel["id"])

    text = (
        "✨ <b>PREMIUM FILE AVAILABLE</b>\n\n"
        f"📁 <b>File:</b> {esc(item['file_name'])}\n"
        f"✍️ <b>Credit:</b> {esc(item['credit'])}\n"
        "🔐 <b>Secure File Access</b>\n\n"
        f"🛡️ <i>{label}</i>"
    )

    # Only a deep-link button is posted. No file or raw file URL.
    await context.bot.send_message(
        chat_id=int(channel["chat_id"]),
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 OPEN FILE", url=link)
        ]]),
        disable_web_page_preview=True
    )


async def publish_selected(update, context, ids):
    q = update.callback_query
    token = context.user_data.get("publish_token")
    item = get_file(token) if token else None

    if not item:
        await q.answer("File not found.", show_alert=True)
        return

    bot = await context.bot.get_me()
    link = f"https://t.me/{bot.username}?start=file_{token}"

    channels = [
        ch for ch in get_channels() if ch["id"] in ids
    ]

    if not channels:
        await q.answer("Select a channel.", show_alert=True)
        return

    await q.answer("Publishing...")
    success = 0
    failed = 0

    for ch in channels:
        try:
            await publish_post(context, ch, item, link)
            success += 1
        except Exception:
            failed += 1
            log.exception("Channel publish failed")

    context.user_data.pop("publish_token", None)
    context.user_data.pop("selected_channels", None)

    await q.message.reply_text(
        f"📢 PUBLISH COMPLETE\n\n"
        f"✅ Success: {success}\n"
        f"❌ Failed: {failed}",
        reply_markup=back_button()
    )


async def publish_router(update, context):
    q = update.callback_query
    data = q.data

    if not is_admin(q.from_user.id):
        await q.answer("Unauthorized", show_alert=True)
        return

    if data == "publish:all":
        await publish_selected(
            update, context,
            {ch["id"] for ch in get_channels()}
        )

    elif data == "publish:choose":
        await q.answer()
        await show_publish_choices(q, context)

    elif data.startswith("publish:toggle:"):
        cid = int(data.split(":")[2])
        selected = context.user_data.setdefault("selected_channels", set())

        if cid in selected:
            selected.remove(cid)
        else:
            selected.add(cid)

        await q.answer("Selection updated")
        await show_publish_choices(q, context)

    elif data == "publish:selected":
        await publish_selected(
            update, context,
            context.user_data.get("selected_channels", set())
        )

    elif data == "publish:cancel":
        context.user_data.pop("publish_token", None)
        context.user_data.pop("selected_channels", None)
        await q.answer("Cancelled")
        await q.message.reply_text("Publishing cancelled.")

    elif data == "publish:back":
        await q.answer()
        await q.message.reply_text(
            "Choose destination:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 ALL CHANNELS", callback_data="publish:all")],
                [InlineKeyboardButton("☑️ CHOOSE CHANNELS", callback_data="publish:choose")],
                [InlineKeyboardButton("❌ CANCEL", callback_data="publish:cancel")]
            ])
        )


async def show_publish_choices(q, context):
    selected = context.user_data.setdefault("selected_channels", set())
    rows = []

    for i, ch in enumerate(get_channels()):
        checked = "✅ " if ch["id"] in selected else "▫️ "
        rows.append([
            InlineKeyboardButton(
                checked + CHANNEL_NAMES[i],
                callback_data=f"publish:toggle:{ch['id']}"
            )
        ])

    rows.append([
        InlineKeyboardButton("📤 PUBLISH SELECTED", callback_data="publish:selected")
    ])
    rows.append([
        InlineKeyboardButton("⬅️ BACK", callback_data="publish:back")
    ])

    await q.message.reply_text(
        "☑️ SELECT CHANNELS",
        reply_markup=InlineKeyboardMarkup(rows)
    )


# ================= BROADCAST =================

async def broadcast_start(update, context):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Unauthorized", show_alert=True)
        return

    context.user_data["mode"] = "broadcast"
    await q.answer()
    await q.message.reply_text(
        "Send the message/media to broadcast to all saved channels.\n"
        "Use /cancel to stop."
    )


async def do_broadcast(message, context):
    success = 0
    failed = 0

    for ch in get_channels():
        try:
            await context.bot.copy_message(
                chat_id=int(ch["chat_id"]),
                from_chat_id=message.chat_id,
                message_id=message.message_id
            )
            success += 1
        except Exception:
            failed += 1
            log.exception("Broadcast failed")

    await message.reply_text(
        f"Broadcast complete.\nSuccess: {success}\nFailed: {failed}",
        reply_markup=admin_keyboard()
    )


# ================= ADMIN STATS =================

async def show_stats(update, context):
    q = update.callback_query

    with db() as con:
        files_count = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        channels_count = con.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        users_count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        deliveries = con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]

    await q.answer()
    await q.message.reply_text(
        "📊 BLACKSHIELD STATISTICS\n\n"
        f"Files: {files_count}\n"
        f"Channels: {channels_count}/{MAX_CHANNELS}\n"
        f"Users: {users_count}\n"
        f"Deliveries: {deliveries}",
        reply_markup=back_button()
    )


# ================= MESSAGE ROUTER =================

async def message_router(update, context):
    m = update.effective_message
    user = update.effective_user
    if not m or not user:
        return

    remember_user(user)
    mode = context.user_data.get("mode")

    if mode == "search":
        context.user_data.pop("mode", None)
        term = (m.text or "").strip()

        with db() as con:
            rows = con.execute("""
            SELECT f.token,f.file_name,MAX(d.delivered_at) AS received
            FROM deliveries d JOIN files f ON f.token=d.token
            WHERE d.user_id=? AND f.file_name LIKE ?
            GROUP BY f.token ORDER BY received DESC LIMIT ?
            """, (user.id, f"%{term}%", PAGE_SIZE)).fetchall()

        if not rows:
            await m.reply_text("No matching files found.")
            return

        buttons = []
        lines = ["🔎 SEARCH RESULTS\n"]

        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {row['file_name']}")
            buttons.append([
                InlineKeyboardButton(
                    f"📥 OPEN {i}",
                    callback_data=f"history:{row['token']}"
                )
            ])

        await m.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if mode == "support":
        context.user_data.pop("mode", None)
        sent = 0

        for aid in ADMINS:
            try:
                await context.bot.send_message(
                    aid,
                    f"🆘 SUPPORT MESSAGE\n\n"
                    f"User ID: {user.id}\n"
                    f"Name: {esc(user.full_name)}\n\n"
                    f"{esc(m.text or m.caption or '[Media message]')}",
                    parse_mode=ParseMode.HTML
                )
                sent += 1
            except Exception:
                log.exception("Support forwarding failed")

        await m.reply_text(
            "✅ Sent to support." if sent else "⚠️ Support unavailable."
        )
        return

    if not is_admin(user.id):
        return

    if mode == "add_channel":
        await detect_channel(m, context)
    elif mode in ("save_file", "save_message", "save_url"):
        await save_content(update, context)
    elif mode == "broadcast":
        if m.text == "/cancel":
            context.user_data.pop("mode", None)
            await m.reply_text("Broadcast cancelled.")
        else:
            context.user_data.pop("mode", None)
            await do_broadcast(m, context)


# ================= CALLBACK ROUTER =================

async def callback_router(update, context):
    q = update.callback_query
    data = q.data
    uid = q.from_user.id

    # User callbacks first.
    if data == "home":
        context.user_data.pop("mode", None)
        await q.answer()
        await q.message.reply_text(
            "🏠 BLACKSHIELD USER MENU",
            reply_markup=user_keyboard(uid)
        )
        return

    if data in ("myfiles", "recent"):
        await history(update, recent=(data == "recent"))
        return

    if data == "search":
        context.user_data["mode"] = "search"
        await q.answer()
        await q.message.reply_text("Send a file name to search.")
        return

    if data == "support":
        context.user_data["mode"] = "support"
        await q.answer()
        await q.message.reply_text("Send your support message.")
        return

    if data == "pending_info":
        await q.answer(
            "Your request is waiting for channel admin approval.",
            show_alert=True
        )
        return

    if data.startswith("verify:"):
        await verify_callback(update, context)
        return

    if data.startswith("history:"):
        token = data.split(":", 1)[1]
        await q.answer()
        # Rechecks all channels again before delivery.
        await deliver_file_from_history(update, context, token)
        return

    if not is_admin(uid):
        await q.answer("Access denied.", show_alert=True)
        return

    if data == "admin":
        await q.answer()
        await q.message.reply_text(
            "🛡️ ADMIN PANEL",
            reply_markup=admin_keyboard()
        )

    elif data == "addchannel":
        await add_channel_start(update, context)

    elif data.startswith("mode:"):
        await choose_join_mode(update, context)

    elif data == "confirm_channel":
        await confirm_channel(update, context)

    elif data == "cancel_channel":
        await cancel_channel(update, context)

    elif data == "channels":
        await show_channels(update, context)

    elif data.startswith("remove:"):
        await remove_channel(update, context)

    elif data == "getlink":
        await getlink_start(update, context)

    elif data.startswith("type:"):
        await choose_type(update, context)

    elif data.startswith("share:"):
        await share_start(update, context)

    elif data.startswith("publish:"):
        await publish_router(update, context)

    elif data == "broadcast":
        await broadcast_start(update, context)

    elif data == "stats":
        await show_stats(update, context)

    else:
        await q.answer("Unknown option.", show_alert=True)


async def deliver_file_from_history(update, context, token):
    item = get_file(token)
    if not item:
        await update.callback_query.message.reply_text("File unavailable.")
        return

    uid = update.effective_user.id
    missing = []

    for ch in get_channels():
        status = await membership_status(context, uid, ch)
        if status != "joined":
            missing.append((ch, status))

    if missing:
        await update.callback_query.message.reply_text(
            "🔐 Membership verification required again.",
            reply_markup=membership_markup(token, missing)
        )
        return

    try:
        await send_item(
            context,
            update.effective_chat.id,
            item
        )
        save_delivery(uid, token)
    except Exception:
        log.exception("History delivery failed")
        await update.callback_query.message.reply_text("Delivery failed.")


# ================= CANCEL / ERROR =================

async def cancel_command(update, context):
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Current action cancelled.",
        reply_markup=(
            admin_keyboard()
            if is_admin(update.effective_user.id)
            else user_keyboard(update.effective_user.id)
        )
    )


async def error_handler(update, context):
    log.error("Update error", exc_info=context.error)


# ================= MAIN =================

def main():
    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN missing. Add it to .env beside main.py."
        )

    if not ADMINS:
        raise RuntimeError(
            "ADMIN_IDS missing. Add your numeric Telegram user ID to .env."
        )

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel_command))

    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, message_router)
    )

    app.add_error_handler(error_handler)

    log.info("BLACKSHIELD FILE BOT started.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
