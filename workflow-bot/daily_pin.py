"""
daily_pin.py
Закрепляет ежедневную сводку релизов (Разработка и ИФТ).
/pinswitch  — вкл/выкл рассылку. Флаг хранится в .pin_flag.json
"""
import datetime as dt, html, json, os
from telegram import Update
from telegram.ext import Application, ContextTypes, CommandHandler
from telegram.error import BadRequest

PIN_CHAT_ID  = int(os.getenv("PIN_CHAT_ID", "0"))
PIN_ENABLED  = os.getenv("PIN_ENABLED", "on") == "on"
PIN_TIME     = dt.time(10, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))  # 09:00 МСК
_FLAG_FILE   = ".pin_flag.json"

def _load_flag():  # persists on disk
    if os.path.exists(_FLAG_FILE):
        try: return json.load(open(_FLAG_FILE))["enabled"]
        except Exception: pass
    return PIN_ENABLED

def _save_flag(v: bool):
    json.dump({"enabled": v}, open(_FLAG_FILE, "w"))

PIN_STATE = _load_flag()

def make_summary(rows):
    def is_dev(r): return "разработка" in str(r.get("Статус")).lower()
    def is_ift(r): return "ифт" in str(r.get("Статус")).lower()
    dev = [r for r in rows if is_dev(r)]
    ift = [r for r in rows if is_ift(r)]
    def line(r):
        name = html.escape(str(r.get("Поставка") or r.get("Release")))
        start = r.get("ИФТ с") or "?"
        end   = r.get("ИФТ по") or "?"
        return f"• <b>{name}</b> — {start} → {end}"
    parts=[]
    if dev: parts.append("<b>Разработка:</b>\n" + "\n".join(line(r) for r in dev))
    if ift: parts.append("<b>ИФТ:</b>\n" + "\n".join(line(r) for r in ift))
    return "\n\n".join(parts) or "Сейчас нет релизов в статусах Разработка и ИФТ"

async def _job(ctx: ContextTypes.DEFAULT_TYPE):
    if not PIN_STATE or PIN_CHAT_ID == 0: return
    today = dt.date.today()
    if today.weekday() >= 5:                      # сб/вс
        return
    rows = ctx.job.data()                        # releases_rows()
    text = make_summary(rows)
    bot = ctx.application.bot
    try: await bot.unpin_all_chat_messages(PIN_CHAT_ID)
    except BadRequest: pass
    msg = await bot.send_message(PIN_CHAT_ID, text, parse_mode="HTML")
    await bot.pin_chat_message(PIN_CHAT_ID, msg.message_id, disable_notification=True)

async def pinswitch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global PIN_STATE
    PIN_STATE = not PIN_STATE
    _save_flag(PIN_STATE)
    await update.message.reply_text(f"Ежедневная сводка {'включена' if PIN_STATE else 'выключена'}.")

def register(app: Application, releases_rows_fn):
    app.job_queue.run_daily(_job, time=PIN_TIME, days=(0,1,2,3,4),
                            name="daily_pin", data=releases_rows_fn)
    app.add_handler(CommandHandler("pinswitch", pinswitch))