import os
import re
import base64
import logging
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mesho-cleaner")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

tg = Application.builder().token(BOT_TOKEN).build()
ai = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# V1 settings are held in memory; they reset when Render restarts the service.
settings = defaultdict(lambda: {"links": True, "media": True, "spam": True})
recent_messages = defaultdict(lambda: deque(maxlen=8))
LINK_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.I)

def is_group(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type in ("group", "supergroup"))

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not is_group(update) or not update.effective_user:
        return False
    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id, update.effective_user.id
        )
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False

async def bot_can_delete(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        return (
            member.status == ChatMemberStatus.ADMINISTRATOR
            and bool(getattr(member, "can_delete_messages", False))
        )
    except Exception:
        return False

async def private_owner(update: Update) -> bool:
    return bool(
        update.effective_user
        and update.effective_user.id == OWNER_ID
        and update.effective_chat
        and update.effective_chat.type == "private"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.effective_message.reply_text(
            "🛡️ Mesho Cleaner يعمل هنا.\n"
            "يجب أن يكون مشرفاً مع صلاحية حذف الرسائل.\n"
            "استخدم /settings لعرض إعدادات الحماية."
        )
    elif await private_owner(update):
        await update.effective_message.reply_text(
            "أهلاً بك في Mesho Cleaner.\n"
            "أضفني إلى مجموعتك واجعلني مشرفاً مع صلاحية حذف الرسائل، "
            "ثم أرسل /settings داخل المجموعة."
        )
    else:
        await update.effective_message.reply_text("هذا البوت مخصص لإدارة المجموعات.")

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    s = settings[update.effective_chat.id]
    await update.effective_message.reply_text(
        "إعدادات الحماية الحالية:\n"
        f"الروابط: {'مفعلة' if s['links'] else 'متوقفة'}\n"
        f"الوسائط: {'مفعلة' if s['media'] else 'متوقفة'}\n"
        f"السبام: {'مفعل' if s['spam'] else 'متوقف'}\n\n"
        "الأوامر: /links_on /links_off /media_on /media_off "
        "/spam_on /spam_off"
    )

async def toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    if not await is_admin(update, context):
        await update.effective_message.reply_text("هذا الأمر للمشرفين فقط.")
        return
    key, value = context.args[0], context.args[1] == "on"
    settings[update.effective_chat.id][key] = value
    await update.effective_message.reply_text(
        f"تم {'تفعيل' if value else 'إيقاف'} حماية {key}."
    )

def make_toggle(key, value):
    async def handler(update, context):
        context.args = [key, value]
        await toggle(update, context)
    return handler

async def classify_image(file_bytes: bytes, mime: str) -> bool:
    """Return True only when moderation flags the image as sexual content."""
    if ai is None:
        return False
    data = base64.b64encode(file_bytes).decode("ascii")
    image_data_url = f"data:{mime};base64,{data}"
    result = await ai.moderations.create(
        model="omni-moderation-latest",
        input=[{
            "type": "image_url",
            "image_url": {"url": image_data_url}
        }]
    )
    item = result.results[0]
    categories = item.categories
    return bool(
        getattr(categories, "sexual", False)
        or getattr(categories, "sexual_minors", False)
    )

async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or chat.type not in ("group", "supergroup") or not user:
        return

    # Never moderate administrators.
    if await is_admin(update, context):
        return

    s = settings[chat.id]

    # Anti-spam: remove a repeated identical text sent repeatedly in a short window.
    text = (msg.text or msg.caption or "").strip()
    if s["spam"] and text:
        history = recent_messages[(chat.id, user.id)]
        if text in history and len(text) >= 3:
            if await bot_can_delete(chat.id, context):
                try:
                    await msg.delete()
                except Exception:
                    pass
                return
        history.append(text)

    # Link protection.
    if s["links"] and text and LINK_RE.search(text):
        if await bot_can_delete(chat.id, context):
            try:
                await msg.delete()
            except Exception:
                pass
        return

    # Media moderation: photos and static stickers only in this first version.
    if s["media"] and (msg.photo or (msg.sticker and not msg.sticker.is_animated and not msg.sticker.is_video)):
        if not ai:
            return  # No AI key: do not delete media based on a guess.
        if not await bot_can_delete(chat.id, context):
            return
        try:
            file_id = msg.photo[-1].file_id if msg.photo else msg.sticker.file_id
            file = await context.bot.get_file(file_id)
            raw = bytes(await file.download_as_bytearray())
            mime = "image/jpeg" if msg.photo else "image/webp"
            if len(raw) > 10 * 1024 * 1024:
                return
            flagged = await classify_image(raw, mime)
            if flagged:
                await msg.delete()
                await context.bot.send_message(
                    chat.id, "🛡️ تم حذف صورة مخالفة وفق إعدادات حماية المجموعة."
                )
        except Exception as exc:
            log.warning("Media moderation failed: %s", exc)

async def health():
    return {"status": "ok", "service": "Mesho Cleaner"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg.initialize()
    await tg.start()
    await tg.bot.set_webhook(
        url=f"{BASE_URL}/telegram-webhook",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
    )
    yield
    await tg.bot.delete_webhook()
    await tg.stop()
    await tg.shutdown()

app = FastAPI(title="Mesho Cleaner", lifespan=lifespan)

@app.get("/")
async def root():
    return await health()

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    update = Update.de_json(data, tg.bot)
    await tg.process_update(update)
    return {"ok": True}

tg.add_handler(CommandHandler("start", start))
tg.add_handler(CommandHandler("settings", settings_cmd))
tg.add_handler(CommandHandler("links_on", make_toggle("links", "on")))
tg.add_handler(CommandHandler("links_off", make_toggle("links", "off")))
tg.add_handler(CommandHandler("media_on", make_toggle("media", "on")))
tg.add_handler(CommandHandler("media_off", make_toggle("media", "off")))
tg.add_handler(CommandHandler("spam_on", make_toggle("spam", "on")))
tg.add_handler(CommandHandler("spam_off", make_toggle("spam", "off")))
tg.add_handler(MessageHandler(
    (filters.TEXT | filters.PHOTO | filters.Sticker.ALL) & ~filters.COMMAND,
    moderate_message
))
