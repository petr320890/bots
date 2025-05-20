"""
release_bot.py • 01-May-2025

Меню релизов из Google-таблицы + авто-сводка (Разработка / ИФТ).

Категории
🗓 Запланированные — статусы 0 «Ожидание» и 1 «В планах»  
✅ Активные        — статусы 2–5 (Анализ / Разработка / ИФТ / ПСИ)  
📁 Прошедшие       — статус 6 «Внедрён»

«👤 Мои релизы» – тот же набор категорий, но только строки, где фамилия
пользователя встречается в колонках Аналитик / Back / Front / QA.
Сопоставление tg-id ↔ фамилия хранится в листе «Ответственные».

Команды
/start         – открыть меню  
/pinactive     – зафиксировать сводку вручную  
/pinswitch     – вкл/выкл ежедневную сводку (будни 10:00 МСК)

Листы в таблице (Google Sheets) — «Релизы», «Release», «Ответственные».
"""

# ─────────────────── импорты ───────────────────────────────────────────────
import os, time, json, html, logging, datetime as dt
from dateutil import parser as dtparse
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials as SAC
from telegram import (
    Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
)
from telegram.ext import (
    Application, ContextTypes,
    CommandHandler, CallbackQueryHandler, ConversationHandler
)
from telegram.error import BadRequest

# ─────────────── .env ───────────────────────────────────────────────────────
load_dotenv()
TOKEN       = os.getenv("TELEGRAM_TOKEN")
SHEET_KEY   = os.getenv("GOOGLE_SHEET_KEY")
CREDS_FILE  = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
PIN_CHAT_ID = int(os.getenv("PIN_CHAT_ID", "0"))
PIN_ENABLED = os.getenv("PIN_ENABLED", "on") == "on"
TASK_SHEET  = os.getenv("TASK_SHEET_NAME", "Release")
if not all([TOKEN, SHEET_KEY, CREDS_FILE]):
    raise RuntimeError("Заполните TELEGRAM_TOKEN, GOOGLE_SHEET_KEY, GOOGLE_APPLICATION_CREDENTIALS")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)s  %(message)s")

# ─────────────── Google Sheets ─────────────────────────────────────────────
SCOPE = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]
CREDS = SAC.from_json_keyfile_name(CREDS_FILE, SCOPE)
GS    = gspread.authorize(CREDS)
ws = lambda n: next((w for w in GS.open_by_key(SHEET_KEY).worksheets()
                     if w.title.strip().lower()==n.lower()), None)

# ─────────────── кеш (3 мин) ───────────────────────────────────────────────
CACHE_TTL=180
_cache={"rel":(0,None),"tasks":(0,None),"owners":(0,None)}
def cached(k, fn):
    ts,data=_cache[k]
    if time.time()-ts>CACHE_TTL or data is None:
        data=fn() or []
        _cache[k]=(time.time(),data)
    return data
releases_rows=lambda: cached("rel",   lambda: ws("Релизы").get_all_records() if ws("Релизы") else [])
tasks_rows   =lambda: cached("tasks", lambda: ws(TASK_SHEET).get_all_records() if ws(TASK_SHEET) else [])
owners_rows  =lambda: cached("owners",lambda: ws("Ответственные").get_all_records() if ws("Ответственные") else [])

# ─────────────── утилиты ───────────────────────────────────────────────────
esc = lambda x: html.escape(str(x or "—"))
norm= lambda s: str(s or "").strip().lower()
STATUS={
    "plan":   ("1.","в планах","0.","ожидание"),
    "active": ("2.","анализ","3.","разработка","4.","ифт","5.","пси"),
    "past":   ("6.","внедрен","внедрён","deployed")
}
def status_check(r, grp): return any(k in norm(r.get("Статус")) for k in STATUS[grp])
title_of = lambda r: r.get("Поставка") or r.get("Release") or "Без названия"
date_safe= lambda r,c: r.get(c) or "—"
content_of=lambda r: r.get("Содержание") or "—"

TODAY = dt.date.today()
def days_phrase(date_str):
    if not date_str: return ""
    try: d=dtparse.parse(str(date_str), dayfirst=True).date()
    except Exception: return ""
    diff=(d - TODAY).days
    if diff>0:  return f"осталось {diff} дн."
    if diff<0:  return f"<b>❗ просрочено на {abs(diff)} дн.</b>"
    return "сегодня последний день"

def user_surname(tid:int):
    for r in owners_rows():
        if str(r.get("tg_id")).strip()==str(tid):
            return r.get("Фамилия","").strip()
    return ""

async def safe_edit(q,*a,**k):
    try: await q.edit_message_text(*a,**k)
    except BadRequest as e:
        if "not modified" in str(e).lower(): await q.answer()
        else: raise

# ─────────────── Conversation states ───────────────────────────────────────
CHOOSING_RELEASE, CHOOSING_ACTION = range(2)

# ─────────────── главное меню ───────────────────────────────────────────────
async def show_main(upd, ctx):
    kb=[
      [Btn("📋 Список релизов", callback_data="list")],
      [Btn("✅ Активные", callback_data="active"),
       Btn("🗓 Запланированные", callback_data="plan")],
      [Btn("📁 Прошедшие", callback_data="past")],
      [Btn("👤 Мои релизы", callback_data="my")]
    ]
    if upd.message:
        await upd.message.reply_text("Выберите категорию:", reply_markup=Markup(kb))
    else:
        await safe_edit(upd.callback_query, "Выберите категорию:", reply_markup=Markup(kb))
    return CHOOSING_RELEASE
async def cmd_start(u,c): return await show_main(u,c)

# ─────────────── фильтр строк ───────────────────────────────────────────────
def filter_rows(rows, view, surname=None):
    if view in STATUS:
        rows=[r for r in rows if status_check(r, view)]
    if surname:
        rows=[r for r in rows if surname.lower() in
             (norm(r.get("Аналитик"))+
              norm(r.get("Back"))+
              norm(r.get("Front"))+
              norm(r.get("QA")))]
    return rows

# ─────────────── список релизов ─────────────────────────────────────────────
async def show_release_list(q, view, surname=None):
    rows=filter_rows(releases_rows(), view, surname)
    if not rows:
        await safe_edit(q,"Ничего не найдено."); return CHOOSING_RELEASE
    kb=[[Btn(title_of(r), callback_data=f"rel:{title_of(r)}")] for r in rows]
    back_cb="back_to_my_menu" if surname else "back_to_menu"
    kb.append([Btn("⬅️ Назад", callback_data=back_cb)])
    await safe_edit(q,"Выберите релиз:", reply_markup=Markup(kb))
    return CHOOSING_ACTION

async def show_my_menu(q):
    kb=[
      [Btn("✅ Активные", callback_data="my_active"),
       Btn("🗓 Запланированные", callback_data="my_plan")],
      [Btn("📁 Прошедшие", callback_data="my_past")],
      [Btn("⬅️ Назад", callback_data="back_to_menu")]
    ]
    await safe_edit(q,"Мои релизы – категория:", reply_markup=Markup(kb))
    return CHOOSING_RELEASE

async def back_to_menu(u,c):
    await u.callback_query.answer()
    return await show_main(u,c)
async def back_to_my_menu(u,c):
    await u.callback_query.answer()
    return await show_my_menu(u.callback_query)

async def choose_release(u,c):
    q=u.callback_query; await q.answer()
    choice=q.data

    # ─── мои релизы (корень) ─────────────────────────────────────────
    if choice == "my":
        surname=user_surname(q.from_user.id)
        if not surname:
            await q.edit_message_text(
                "⚠️ В листе «Ответственные» нет вашего tg_id.\n"
                "Попросите администратора добавить запись.")
            return CHOOSING_RELEASE
        c.user_data.update(mode="my", surname=surname)
        return await show_my_menu(q)

    # ─── мои релизы → конкретная категория ───────────────────────────
    if choice.startswith("my_"):
        view=choice[3:]
        surname=c.user_data["surname"]
        c.user_data.update(view=view)
        return await show_release_list(q, view, surname)

    # ─── общие категории ─────────────────────────────────────────────
    c.user_data.update(mode="common", view=choice, surname=None)
    return await show_release_list(q, choice)

# ─────────────── меню действий релиза ───────────────────────────────────────
async def show_action_menu(q, rel):
    kb=[
      [Btn("📊 Статус", callback_data="act:status")],
      [Btn("👥 Ответственные", callback_data="act:owners")],
      [Btn("📅 Этапы / сроки", callback_data="act:stages")],
      [Btn("🗒 Задачи", callback_data="act:tasks")],
      [Btn("⬅️ К релизам", callback_data="back_to_releases")]
    ]
    await safe_edit(q,f"Вы выбрали <b>{esc(rel)}</b>.",
                    reply_markup=Markup(kb), parse_mode="HTML")
    return CHOOSING_ACTION

# ─────────────── карточки ──────────────────────────────────────────────────
async def choose_action(u,c):
    q=u.callback_query; await q.answer()
    d=q.data
    if d=="back_to_releases":
        return await show_release_list(q, c.user_data["view"], c.user_data.get("surname"))
    if d=="back_to_my_menu":
        return await show_my_menu(q)
    if d=="back_to_actions":
        return await show_action_menu(q, c.user_data["rel"])
    if d.startswith("rel:"):
        c.user_data["rel"]=rel=d[4:]
        return await show_action_menu(q,rel)

    rel_name=c.user_data["rel"]
    rel=next((r for r in releases_rows() if title_of(r)==rel_name), None)
    if not rel:
        await safe_edit(q,"Не удалось найти релиз."); return CHOOSING_RELEASE

    if d=="act:status":
        text=(f"<b>{esc(rel_name)}</b>\n{esc(content_of(rel))}\n\n"
              f"<b>Статус:</b> {esc(rel.get('Статус'))}\n"
              f"<b>Дедлайн:</b> {esc(date_safe(rel,'релиз'))}")
    elif d=="act:owners":
        text="\n".join(f"<b>{f}:</b> {esc(rel.get(f))}"
                       for f in ("Аналитик","Back","Front","QA"))
    elif d=="act:stages":
        text=(f"<b>Аналитика до:</b>  {esc(date_safe(rel,'SA'))}\n"
              f"<b>Разработка до:</b> {esc(date_safe(rel,'ИФТ с'))}\n"
              f"<b>ИФТ:</b>            {esc(date_safe(rel,'ИФТ с'))} → {esc(date_safe(rel,'ИФТ по'))}\n"
              f"<b>ПСИ до:</b>        {esc(date_safe(rel,'ПСИ'))}\n"
              f"<b>Релиз до:</b>      {esc(date_safe(rel,'релиз'))}")
    else:
        rows=[t for t in tasks_rows() if norm(t.get("Release"))==norm(rel_name)]
        text=("\n".join(
            f"• <b>{esc(t['Story'])}</b> | {esc(t['Task'])} | <i>{esc(t['Status'])}</i>"
            for t in rows[:40]) or "Задач нет.")

    await safe_edit(q, text,
        reply_markup=Markup([[Btn("⬅️ Назад", callback_data="back_to_actions")]]),
        parse_mode="HTML")
    return CHOOSING_ACTION
async def cancel(u,c): await u.message.reply_text("Диалог завершён.")

# ─────────────── сводка «Разработка / ИФТ» ─────────────────────────────────
_FLAG=".pin_flag.json"
def _load_flag():
    if os.path.exists(_FLAG):
        try: return json.load(open(_FLAG))["enabled"]
        except Exception: pass
    return PIN_ENABLED
def _save_flag(v): json.dump({"enabled":v}, open(_FLAG,"w"))
PIN_STATE=_load_flag()

def make_summary():
    rows=releases_rows()
    dev=[r for r in rows if "разработка" in norm(r.get("Статус"))]
    ift=[r for r in rows if "ифт"         in norm(r.get("Статус"))]

    dev_line=lambda r: f"• <b>{esc(title_of(r))}</b> — до {esc(r.get('ИФТ с'))} {days_phrase(r.get('ИФТ с'))}"
    ift_line=lambda r: f"• <b>{esc(title_of(r))}</b> — {esc(r.get('ИФТ с'))} → {esc(r.get('ИФТ по'))} {days_phrase(r.get('ИФТ по'))}"

    parts=[]
    if dev: parts.append("<b>Разработка:</b>\n"+ "\n".join(dev_line(r) for r in dev))
    if ift: parts.append("<b>ИФТ:</b>\n"+ "\n".join(ift_line(r) for r in ift))
    return "\n\n".join(parts) or "Сейчас нет релизов в статусах Разработка и ИФТ"

async def _pin(bot):
    if PIN_CHAT_ID==0:
        logging.warning("PIN_CHAT_ID=0 – сводка не отправлена")
        return
    summary=make_summary()
    try: await bot.unpin_all_chat_messages(PIN_CHAT_ID)
    except BadRequest: pass
    msg=await bot.send_message(PIN_CHAT_ID, summary, parse_mode="HTML")
    await bot.pin_chat_message(PIN_CHAT_ID, msg.message_id, disable_notification=True)

async def daily_pin(ctx: ContextTypes.DEFAULT_TYPE):
    if not PIN_STATE or PIN_CHAT_ID==0: return
    if dt.date.today().weekday()>=5: return
    await _pin(ctx.bot)
async def pin_now(upd:Update,ctx): await _pin(ctx.bot) or upd.message.reply_text("Сводка закреплена.")
async def pinswitch(upd:Update,_):
    global PIN_STATE
    PIN_STATE=not PIN_STATE; _save_flag(PIN_STATE)
    await upd.message.reply_text(f"Авто-сводка {'включена' if PIN_STATE else 'выключена'}.")

# ─────────────── main ───────────────────────────────────────────────────────
def main():
    app=Application.builder().token(TOKEN).build()

    conv=ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            CHOOSING_RELEASE:[
                CallbackQueryHandler(choose_release,
                    pattern="^(list|active|past|plan|my(_.*)?|back_to_my_menu)$"),
                CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$")
            ],
            CHOOSING_ACTION:[
                CallbackQueryHandler(choose_action,
                    pattern="^(rel:.*|act:.*|back_to_releases|back_to_actions|back_to_my_menu)$"),
                CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$")
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("pinactive", pin_now))
    app.add_handler(CommandHandler("pinswitch", pinswitch))

    app.job_queue.run_daily(
        daily_pin,
        time=dt.time(10,0,tzinfo=dt.timezone(dt.timedelta(hours=3))),
        days=(0,1,2,3,4), name="daily_pin")

    logging.info("🚀 Бот запущен")
    app.run_polling(poll_interval=1, timeout=30)

if __name__ == "__main__":
    main()