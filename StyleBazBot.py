"""
R-Gen Style Text Telegram Bot
Each style result is its own message with a [📋 Copy] button.
Pressing the button sends that styled text as a plain message so
the user can long-press → Copy in Telegram.
"""

import logging
import asyncio
import aiohttp
from urllib.parse import quote
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# ──────────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────────
BOT_TOKEN = "8943230974:AAH1aLHDHQkSVmGrTHMkU8DYkWm4TNMQD4I"
ADMIN_ID  = 8690101844
API_BASE  = "https://style-text-gen.vercel.app/api/style"

# How many style cards to send per page
PER_PAGE = 10

# Conversation states
WAITING_TEXT      = 1
WAITING_BROADCAST = 2

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
#  IN-MEMORY USER DB  (swap for SQLite in production)
# ──────────────────────────────────────────────────────────
user_db: set[int] = set()

def register_user(uid: int):
    user_db.add(uid)

# ──────────────────────────────────────────────────────────
#  STATIC TEXTS
# ──────────────────────────────────────────────────────────
WELCOME_TEXT = (
    "✨ *R-Gen Style Text Bot*\n\n"
    "Transform your text into *1000+ premium fonts*, "
    "Zalgo effects & Crypto Ciphers.\n\n"
    "👇 Choose a language to begin:"
)

HELP_TEXT = (
    "📖 *How to Use*\n\n"
    "1️⃣ Choose a language from the menu\n"
    "2️⃣ Type and send your text\n"
    "3️⃣ Each style appears as a card\n"
    "4️⃣ Tap 📋 *Copy* on any card → bot sends that text\n"
    "5️⃣ Long-press the sent text → *Copy*\n\n"
    "🔹 *Commands*\n"
    "/start – Home menu\n"
    "/generate – Start generating\n"
    "/help – This screen\n"
    "/cancel – Cancel current action"
)

# ──────────────────────────────────────────────────────────
#  KEYBOARDS
# ──────────────────────────────────────────────────────────
def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🔤 English",  callback_data="lang_en"),
            InlineKeyboardButton("🇧🇩 Bangla",  callback_data="lang_bn"),
            InlineKeyboardButton("🇮🇳 Hindi",   callback_data="lang_hi"),
        ],
        [
            InlineKeyboardButton("🇸🇦 Arabic",  callback_data="lang_ar"),
            InlineKeyboardButton("🇷🇺 Russian", callback_data="lang_ru"),
        ],
        [
            InlineKeyboardButton("ℹ️ Help",     callback_data="help"),
            InlineKeyboardButton("📊 Stats",    callback_data="stats"),
        ],
    ]
    if is_admin:
        rows.append([
            InlineKeyboardButton("📢 Broadcast",  callback_data="broadcast"),
            InlineKeyboardButton("👥 User Count", callback_data="usercount"),
        ])
    return InlineKeyboardMarkup(rows)

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 Main Menu", callback_data="mainmenu")
    ]])

def waiting_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🌐 Change Language", callback_data="changelang"),
        InlineKeyboardButton("❌ Cancel",           callback_data="mainmenu"),
    ]])

def lang_select_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔤 English",  callback_data="lang_en"),
            InlineKeyboardButton("🇧🇩 Bangla",  callback_data="lang_bn"),
            InlineKeyboardButton("🇮🇳 Hindi",   callback_data="lang_hi"),
        ],
        [
            InlineKeyboardButton("🇸🇦 Arabic",  callback_data="lang_ar"),
            InlineKeyboardButton("🇷🇺 Russian", callback_data="lang_ru"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="mainmenu")],
    ])

def style_card_kb(style_index: int) -> InlineKeyboardMarkup:
    """Each style card has one Copy button."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Copy Text", callback_data=f"copy_{style_index}"),
    ]])

def pagination_kb(page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Navigation row sent after each page of cards."""
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page + 1}"))

    return InlineKeyboardMarkup([
        nav,
        [
            InlineKeyboardButton("🔄 New Text",   callback_data="changelang"),
            InlineKeyboardButton("🏠 Menu",        callback_data="mainmenu"),
        ],
    ])

# ──────────────────────────────────────────────────────────
#  API FETCH
# ──────────────────────────────────────────────────────────
async def fetch_styles(text: str, lang: str) -> dict | None:
    url = f"{API_BASE}?text={quote(text)}&lang={lang}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logger.error(f"API error: {e}")
    return None

# ──────────────────────────────────────────────────────────
#  SEND ONE PAGE OF STYLE CARDS
# ──────────────────────────────────────────────────────────
async def send_style_page(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    data: dict,
    page: int,
    reply_to: int | None = None,
):
    """Send PER_PAGE style cards + a navigation message."""
    styles      = data.get("data", [])
    total       = len(styles)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    start       = page * PER_PAGE
    end         = min(start + PER_PAGE, total)
    chunk       = styles[start:end]

    # Store flat styles list in bot_data for copy lookup (keyed by chat_id)
    context.bot_data.setdefault("styles", {})[chat_id] = styles

    meta     = data.get("meta", {})
    analysis = data.get("analysis", {})

    # Header summary (only on page 0)
    if page == 0:
        summary = (
            f"✅ *{meta.get('total_styles_generated', total)} styles generated!*\n"
            f"📝 Chars: `{analysis.get('character_count', '?')}` | "
            f"Words: `{analysis.get('word_count', '?')}` | "
            f"⏱ `{meta.get('processing_time_ms', '?')}ms`"
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=summary,
            parse_mode=ParseMode.MARKDOWN,
        )

    # One message per style card
    for i, item in enumerate(chunk):
        global_index = start + i
        category     = item.get("category", "Style")
        name         = item.get("name", "Custom")
        result       = item.get("result", "")

        card_text = (
            f"🏷 *{name}*  `[{category}]`\n\n"
            f"{result}"
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=card_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=style_card_kb(global_index),
        )
        # Small delay to avoid flood limits when many cards
        await asyncio.sleep(0.07)

    # Navigation footer
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📄 Page *{page + 1}* of *{total_pages}* — {total} styles total",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=pagination_kb(page, total_pages),
    )

# ──────────────────────────────────────────────────────────
#  COMMAND HANDLERS
# ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id)
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(user.id == ADMIN_ID),
    )
    return ConversationHandler.END

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id)
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb(),
    )
    return ConversationHandler.END

async def cmd_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id)
    context.user_data["lang"] = "en"
    await update.message.reply_text(
        "✏️ *Send your text now!*\n_Language: Auto/English_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=waiting_kb(),
    )
    return WAITING_TEXT

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=main_menu_kb(uid == ADMIN_ID),
    )
    return ConversationHandler.END

# ──────────────────────────────────────────────────────────
#  CALLBACK / BUTTON HANDLER
# ──────────────────────────────────────────────────────────
LANG_NAMES = {
    "en": "Auto/English", "bn": "Bangla",
    "hi": "Hindi",        "ar": "Arabic", "ru": "Russian",
}

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    data     = query.data
    uid      = query.from_user.id
    chat_id  = query.message.chat_id
    is_admin = (uid == ADMIN_ID)
    register_user(uid)

    # ── noop (page indicator button) ──────────────────────
    if data == "noop":
        return

    # ── Main menu ─────────────────────────────────────────
    if data == "mainmenu":
        await query.edit_message_text(
            WELCOME_TEXT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(is_admin),
        )
        return ConversationHandler.END

    # ── Help ──────────────────────────────────────────────
    if data == "help":
        await query.edit_message_text(
            HELP_TEXT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return ConversationHandler.END

    # ── Stats ─────────────────────────────────────────────
    if data == "stats":
        await query.edit_message_text(
            f"📊 *Bot Statistics*\n\n"
            f"👥 Total Users: `{len(user_db)}`\n"
            f"🤖 *R-Gen Style Text Bot*\n"
            f"🌐 API: `style-text-gen.vercel.app`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return ConversationHandler.END

    # ── Admin: user count ─────────────────────────────────
    if data == "usercount" and is_admin:
        await query.edit_message_text(
            f"👥 *Registered Users:* `{len(user_db)}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return ConversationHandler.END

    # ── Admin: broadcast ──────────────────────────────────
    if data == "broadcast" and is_admin:
        await query.edit_message_text(
            "📢 *Broadcast Mode*\n\n"
            "Type the message you want to send to all users.\n"
            "_(/cancel to abort)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="mainmenu")
            ]]),
        )
        return WAITING_BROADCAST

    # ── Change language ───────────────────────────────────
    if data == "changelang":
        await query.edit_message_text(
            "🌐 *Select Language:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=lang_select_kb(),
        )
        return WAITING_TEXT

    # ── Language selected → ask for text ──────────────────
    if data.startswith("lang_"):
        lang_code = data[5:]
        context.user_data["lang"] = lang_code
        await query.edit_message_text(
            f"✏️ *Send your text now!*\n_Language: {LANG_NAMES.get(lang_code, lang_code)}_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=waiting_kb(),
        )
        return WAITING_TEXT

    # ── Pagination ────────────────────────────────────────
    if data.startswith("page_"):
        page   = int(data[5:])
        cached = context.user_data.get("last_result")
        if cached:
            await query.edit_message_text(
                f"⏳ Loading page {page + 1}...",
                parse_mode=ParseMode.MARKDOWN,
            )
            await send_style_page(context, chat_id, cached, page)
        return WAITING_TEXT

    # ── COPY button ───────────────────────────────────────
    if data.startswith("copy_"):
        idx    = int(data[5:])
        styles = context.bot_data.get("styles", {}).get(chat_id, [])
        if 0 <= idx < len(styles):
            result_text = styles[idx].get("result", "")
            name        = styles[idx].get("name", "Style")
            # Send the raw styled text as a new message → user can long-press & copy
            await context.bot.send_message(
                chat_id=chat_id,
                text=result_text,
            )
            await query.answer(f"✅ {name} sent! Long-press to copy.", show_alert=False)
        else:
            await query.answer("⚠️ Style not found. Please regenerate.", show_alert=True)
        return WAITING_TEXT

    return ConversationHandler.END

# ──────────────────────────────────────────────────────────
#  TEXT INPUT HANDLER  (user sends text to style)
# ──────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    lang      = context.user_data.get("lang", "en")
    chat_id   = update.effective_chat.id

    processing = await update.message.reply_text(
        "⏳ *Generating styles...*",
        parse_mode=ParseMode.MARKDOWN,
    )

    result = await fetch_styles(user_text, lang)

    await processing.delete()

    if not result or not result.get("success"):
        await update.message.reply_text(
            "❌ *Failed to generate styles.* Please try again.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(),
        )
        return WAITING_TEXT

    context.user_data["last_result"] = result
    await send_style_page(context, chat_id, result, page=0)
    return WAITING_TEXT

# ──────────────────────────────────────────────────────────
#  BROADCAST HANDLER  (admin only)
# ──────────────────────────────────────────────────────────
async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    text = update.message.text.strip()
    if text.lower() in ["/cancel", "cancel"]:
        await update.message.reply_text(
            "❌ Broadcast cancelled.",
            reply_markup=main_menu_kb(is_admin=True),
        )
        return ConversationHandler.END

    status = await update.message.reply_text(
        f"📢 Sending to *{len(user_db)}* users…",
        parse_mode=ParseMode.MARKDOWN,
    )

    ok = fail = 0
    for uid in list(user_db):
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"📢 *Message from Admin*\n\n{text}",
                parse_mode=ParseMode.MARKDOWN,
            )
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"✅ *Broadcast done!*\n\n✔️ Delivered: `{ok}`\n❌ Failed: `{fail}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_kb(is_admin=True),
    )
    return ConversationHandler.END

# ──────────────────────────────────────────────────────────
#  FALLBACK  (outside conversation)
# ──────────────────────────────────────────────────────────
async def fallback_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid)
    await update.message.reply_text(
        "💡 Use /generate or pick a language below.",
        reply_markup=main_menu_kb(uid == ADMIN_ID),
    )

# ──────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # ── Broadcast conversation ────────────────────────────
    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^broadcast$")],
        states={
            WAITING_BROADCAST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_broadcast),
                CallbackQueryHandler(button_handler, pattern="^mainmenu$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel), CommandHandler("start", cmd_start)],
        per_user=True, per_chat=True,
    )

    # ── Style-generation conversation ─────────────────────
    style_conv = ConversationHandler(
        entry_points=[
            CommandHandler("generate", cmd_generate),
            CallbackQueryHandler(button_handler, pattern="^lang_"),
            CallbackQueryHandler(button_handler, pattern="^changelang$"),
        ],
        states={
            WAITING_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
                CallbackQueryHandler(button_handler),
            ],
        },
        fallbacks=[
            CommandHandler("start",  cmd_start),
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("help",   cmd_help),
        ],
        per_user=True, per_chat=True,
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("cancel",   cmd_cancel))
    app.add_handler(broadcast_conv)
    app.add_handler(style_conv)
    app.add_handler(CallbackQueryHandler(button_handler))   # fallback callbacks
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_msg))

    logger.info("🚀 R-Gen Style Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
