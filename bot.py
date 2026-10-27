# -*- coding: utf-8 -*-
"Telegram-бот @test2_for_work_bot"
import asyncio
import csv
import json
import logging
import os
import re
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from scenario import BACK, GROUPS, STEPS

BASE = Path(__file__).parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
OVERRIDES_FILE, SETTINGS_FILE = DATA / "overrides.json", DATA / "settings.json"
LEADS_FILE, PRICES_FILE = DATA / "leads.csv", BASE / "prices.json"

log = logging.getLogger("bot")
router = Router()


# ───────────────────────── Хранилище ─────────────────────────
def load_json(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


overrides: dict = load_json(OVERRIDES_FILE, {})   # тексты, изменённые через /admin
settings: dict = load_json(SETTINGS_FILE, {})     # chat_id менеджера и т.п.


def step_text(step):
    return overrides.get(step["id"], step["text"])


def is_admin(uid):
    ids = {x for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x}
    return str(uid) in ids


def manager_chat():
    return settings.get("manager_chat") or os.getenv("MANAGER_CHAT_ID") or None


# ───────────────────────── Сессии ─────────────────────────
sessions: dict = {}


def sess(user):
    s = sessions.setdefault(user.id, {"stack": [], "ans": {}, "files": [], "ask": None,
                                      "submitted": False, "adm_edit": None})
    s["user"] = {"id": user.id, "username": user.username, "name": user.full_name}
    return s


def push(s, sid):
    st = s["stack"]
    if sid in st:                       # возврат к уже пройденному шагу (например, к списку разделов)
        del st[st.index(sid) + 1:]
    else:
        st.append(sid)


def client_name(s):
    a = s["ans"].get("f_name")
    return a["sel"][0] if a and a["sel"] else ""


def keyboard(step, s):
    rows, a = [], s["ans"].get(step["id"], {})
    for i, opt in enumerate(step["options"]):
        label = opt["label"]
        if step["type"] == "multi":
            label = ("✅ " if opt["label"] in a.get("sel", []) else "⬜ ") + label
        rows.append([InlineKeyboardButton(text=label, callback_data=f"o:{i}")])
    if step["type"] == "multi":
        rows.append([InlineKeyboardButton(text=step.get("done_label", "➡️ Далее"), callback_data="done")])
    if step.get("back", True) and len(s["stack"]) > 1:
        rows.append([InlineKeyboardButton(text=BACK, callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show(msg: Message, s, sid, edit=True):
    step = STEPS[sid]
    text = step_text(step).replace("{name}", client_name(s))
    markup = keyboard(step, s)
    if edit:
        try:
            await msg.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    await msg.answer(text, reply_markup=markup)


async def go(msg: Message, s, sid, edit=True):
    if sid == "main":                   # главное меню = начать заявку заново
        s.update(stack=["main"], ans={}, files=[], ask=None, submitted=False)
    else:
        push(s, sid)
    if sid == "f_done" and not s["submitted"]:
        s["submitted"] = True
        await submit_lead(msg.bot, s)
    await show(msg, s, sid, edit)


def next_of(step, opt, s):
    n = (opt or {}).get("next") or step.get("next")
    return n(s) if callable(n) else n


# ───────────────────────── Заявка: сводка, оценка, отправка ─────────────────────────
FLOW_NAMES = {"p": "Новый сайт — товары", "s": "Новый сайт — услуги", "m": "Новый сайт — товары и услуги",
              "r": "Доработка сайта", "pr": "Продвижение сайта", "n": "Новый сайт (своё описание)",
              "ex": "Запрос примеров работ"}


def detect_flow(ans):
    prefixes = {k.split("_", 1)[0] for k in ans}
    for p in ("p", "s", "m", "r", "pr", "n", "ex"):
        if p in prefixes:
            return p
    return ""


def summary_lines(ans):
    lines = []
    for key, a in ans.items():
        if key.startswith("f_"):
            continue
        extra = a.get("extra", {})
        parts = [f"{x} ({extra[x]})" if x in extra else x for x in a["sel"]]
        parts += [f"{k}: {v}" for k, v in extra.items() if k not in a["sel"]]
        if parts:
            lines.append(f"• {a['title']}: {', '.join(parts)}")
    return lines


def estimate(ans):
    cfg = load_json(PRICES_FILE, {})
    code = detect_flow(ans)
    if code not in cfg.get("base", {}):
        return None
    total = cfg["base"][code]
    for key, a in ans.items():
        suffix = key.split("_", 1)[1]
        for r in cfg.get("rules", []):
            if not fnmatch(suffix, r["step"]):
                continue
            if "per_item" in r:
                total += r["per_item"] * len([x for x in a["sel"] if x.strip() != "Нет"])
            elif any(r["answer"] in x for x in a["sel"]):
                total += r["add"]
    lo, hi = cfg.get("range", [0.85, 1.3])
    rnd = lambda v: int(round(v / 5000) * 5000)
    fmt = lambda v: f"{v:,}".replace(",", " ")
    return f"{fmt(rnd(total * lo))} – {fmt(rnd(total * hi))} {cfg.get('currency', '₽')}"


def chunks(text, size=3900):
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > size:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf.strip():
        yield buf


async def submit_lead(bot: Bot, s):
    ans = s["ans"]
    first = lambda k: ans[k]["sel"][0] if k in ans and ans[k]["sel"] else ""
    name, phone, when = first("f_name"), first("f_phone"), first("f_time")
    messengers = ", ".join(ans.get("f_msg", {}).get("sel", []))
    flow = FLOW_NAMES.get(detect_flow(ans), "Обращение без выбранной ветки")
    est = estimate(ans)
    u = s["user"]
    tg = f"@{u['username']}" if u.get("username") else f"id {u['id']}"
    lines = summary_lines(ans)

    card = (f"🔔 Новая заявка\n\n👤 {name}\n📞 {phone}\n💬 Мессенджеры: {messengers or '—'}\n"
            f"🕐 Удобное время: {when or '—'}\n✈️ Telegram: {tg}\n📌 Ветка: {flow}\n")
    if est:
        card += f"💰 Предварительная вилка: {est} (ориентир, не оферта)\n"
    card += "\nОтветы клиента:\n" + "\n".join(lines)

    new_file = not LEADS_FILE.exists()
    with LEADS_FILE.open("a", newline="", encoding="utf-8-sig") as f:   # utf-8-sig — чтобы Excel не ломал кириллицу
        w = csv.writer(f, delimiter=";")
        if new_file:
            w.writerow(["Дата", "Имя", "Телефон", "Мессенджеры", "Время", "Telegram", "Ветка", "Оценка", "Ответы"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), name, phone, messengers, when, tg,
                    flow, est or "", " | ".join(lines)])

    chat = manager_chat()
    if not chat:
        log.warning("Менеджер не задан: отправьте боту /setmanager. Заявка сохранена в data/leads.csv")
        return
    try:
        for part in chunks(card):
            await bot.send_message(chat, part)
        for kind, fid in s["files"]:
            await (bot.send_document if kind == "document" else bot.send_photo)(chat, fid)
    except Exception:
        log.exception("Не удалось отправить заявку менеджеру")


# ───────────────────────── Команды клиента ─────────────────────────
@router.message(CommandStart())
async def cmd_start(m: Message):
    sessions.pop(m.from_user.id, None)
    s = sess(m.from_user)
    await go(m, s, "privacy", edit=False)


@router.message(Command("menu"))
async def cmd_menu(m: Message):
    await go(m, sess(m.from_user), "main", edit=False)


@router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"Ваш Telegram ID: {m.from_user.id}\nID этого чата: {m.chat.id}")


# ───────────────────────── Админ-панель ─────────────────────────
def admin_home():
    return ("🔧 Панель администратора", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Тексты сообщений", callback_data="adm:groups")],
        [InlineKeyboardButton(text="📬 Куда приходят заявки", callback_data="adm:mgr")]]))


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id):
        await m.answer(f"Нет доступа. Ваш ID: {m.from_user.id}.\nЧтобы стать администратором, впишите "
                       "этот ID в ADMIN_IDS в файле .env и перезапустите бота.")
        return
    text, markup = admin_home()
    await m.answer(text, reply_markup=markup)


@router.message(Command("setmanager"))
async def cmd_setmanager(m: Message):
    if not is_admin(m.from_user.id):
        return
    settings["manager_chat"] = m.chat.id
    save_json(SETTINGS_FILE, settings)
    await m.answer("✅ Готово. Новые заявки будут приходить в этот чат.")


def btn(text, data):
    return [InlineKeyboardButton(text=text, callback_data=data)]


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    s = sess(cb.from_user)
    _, act, *rest = cb.data.split(":", 2)
    arg = rest[0] if rest else ""
    groups = list(GROUPS)
    s["adm_edit"] = None
    if act == "home":
        text, markup = admin_home()
    elif act == "groups":
        text = "Выберите раздел сценария:"
        markup = InlineKeyboardMarkup(inline_keyboard=[btn(g, f"adm:g:{i}") for i, g in enumerate(groups)]
                                      + [btn(BACK, "adm:home")])
    elif act == "g":
        g = groups[int(arg)]
        text = f"{g}\nВыберите сообщение для редактирования (✏️ — уже изменено вами):"
        rows = [btn(("✏️ " if sid in overrides else "") + STEPS[sid]["title"][:55], f"adm:s:{sid}")
                for sid in GROUPS[g]]
        markup = InlineKeyboardMarkup(inline_keyboard=rows + [btn(BACK, "adm:groups")])
    elif act in ("s", "r"):
        if act == "r":
            overrides.pop(arg, None)
            save_json(OVERRIDES_FILE, overrides)
        st = STEPS[arg]
        text = f"«{st['title']}»\n\nТекущий текст:\n\n{step_text(st)[:3500]}"
        rows = [btn("✏️ Изменить текст", f"adm:e:{arg}")]
        if arg in overrides:
            rows.append(btn("↩️ Вернуть исходный текст", f"adm:r:{arg}"))
        rows.append(btn(BACK, f"adm:g:{list(GROUPS).index(st['group'])}"))
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
    elif act == "e":
        s["adm_edit"] = arg
        text = ("Отправьте новый текст одним сообщением.\n"
                + ("Можно использовать {name} — туда подставится имя клиента.\n" if arg == "f_done" else "")
                + "Эмодзи и переносы строк сохранятся.")
        markup = InlineKeyboardMarkup(inline_keyboard=[btn("Отмена", f"adm:s:{arg}")])
    elif act == "mgr":
        cur = manager_chat()
        text = (f"Заявки приходят в чат: {cur or 'не задан'}.\n\nЧтобы заявки приходили в другой чат "
                "(личный, группа), напишите команду /setmanager в нужном чате.\n"
                "Все заявки также сохраняются в файл data/leads.csv (открывается в Excel).")
        markup = InlineKeyboardMarkup(inline_keyboard=[btn(BACK, "adm:home")])
    else:
        await cb.answer()
        return
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await cb.answer()


# ───────────────────────── Кнопки сценария ─────────────────────────
async def guard(cb: CallbackQuery):
    s = sess(cb.from_user)
    if not s["stack"]:
        await cb.answer("Сессия устарела, отправьте /start", show_alert=True)
        return None
    return s


@router.callback_query(F.data == "back")
async def cb_back(cb: CallbackQuery):
    if not (s := await guard(cb)):
        return
    s["ask"] = None
    if len(s["stack"]) > 1:
        s["stack"].pop()
    await show(cb.message, s, s["stack"][-1])
    await cb.answer()


@router.callback_query(F.data == "cancelask")
async def cb_cancelask(cb: CallbackQuery):
    if not (s := await guard(cb)):
        return
    s["ask"] = None
    await show(cb.message, s, s["stack"][-1])
    await cb.answer()


@router.callback_query(F.data == "done")
async def cb_done(cb: CallbackQuery):
    if not (s := await guard(cb)):
        return
    s["ask"] = None
    step = STEPS[s["stack"][-1]]
    await go(cb.message, s, next_of(step, None, s))
    await cb.answer()


@router.callback_query(F.data.startswith("o:"))
async def cb_option(cb: CallbackQuery):
    if not (s := await guard(cb)):
        return
    step = STEPS[s["stack"][-1]]
    i = int(cb.data[2:])
    if i >= len(step["options"]):
        await cb.answer()
        return
    opt, sid = step["options"][i], step["id"]
    label = opt["label"]

    if step["type"] == "multi":
        a = s["ans"].setdefault(sid, {"title": step["title"], "sel": [], "extra": {}})
        excl = {x["label"] for x in step["options"] if x["exclusive"]}
        if label in a["sel"]:
            a["sel"].remove(label)
            a["extra"].pop(label, None)
        else:
            if opt["exclusive"]:
                a["sel"].clear()
                a["extra"].clear()
            else:
                a["sel"] = [x for x in a["sel"] if x not in excl]
                for x in excl:
                    a["extra"].pop(x, None)
            a["sel"].append(label)
        s["ask"] = {"step": sid, "opt": i} if opt["ask"] and label in a["sel"] else None
        await show(cb.message, s, sid)
        if s["ask"]:
            await cb.message.answer(opt["ask"])
        await cb.answer()
        return

    s["ask"] = None
    if step["record"] and opt["rec"]:
        for pref in step.get("purge", ()):      
            for k in [k for k in s["ans"] if k.startswith(pref)]:
                del s["ans"][k]
        s["ans"][sid] = {"title": step["title"], "sel": [label], "extra": {}}
    if opt["ask"]:
        s["ask"] = {"step": sid, "opt": i}
        await cb.message.edit_text(opt["ask"], reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[btn(BACK, "cancelask")]))
        await cb.answer()
        return
    nxt = next_of(step, opt, s)
    if opt["say"]:
        await cb.message.edit_text(opt["say"])
        await go(cb.message, s, nxt, edit=False)
    else:
        await go(cb.message, s, nxt)
    await cb.answer()


# ───────────────────────── Текстовые сообщения ─────────────────────────
def extract(m: Message, s):
    """Достаёт текст/файл из сообщения клиента."""
    note = m.caption or ""
    if m.text:
        return m.text.strip()
    if m.document:
        s["files"].append(("document", m.document.file_id))
        return f"[файл: {m.document.file_name}] {note}".strip()
    if m.photo:
        s["files"].append(("photo", m.photo[-1].file_id))
        return f"[фото] {note}".strip()
    return None


@router.message()
async def on_message(m: Message):
    s = sess(m.from_user)

    if s.get("adm_edit") and is_admin(m.from_user.id) and m.text:       # администратор правит текст
        sid, s["adm_edit"] = s["adm_edit"], None
        overrides[sid] = m.text
        save_json(OVERRIDES_FILE, overrides)
        await m.answer("✅ Текст сохранён. Бот уже использует новый вариант.\nОткрыть /admin")
        return

    if not s["stack"]:
        await cmd_start(m)
        return
    step = STEPS[s["stack"][-1]]
    value = extract(m, s)
    if value is None:
        await m.answer("Пожалуйста, отправьте текст или файл.")
        return

    if s["ask"]:                                    # клиент отвечает на «Напишите…»
        st = STEPS[s["ask"]["step"]]
        opt = st["options"][s["ask"]["opt"]]
        a = s["ans"].setdefault(st["id"], {"title": st["title"], "sel": [], "extra": {}})
        a["extra"][opt["label"]] = value
        s["ask"] = None
        if st["type"] == "multi":
            await m.answer("Принято ✅ Продолжайте выбор или нажмите «Далее».")
        else:
            await go(m, s, next_of(st, opt, s), edit=False)
    elif step.get("accept_text"):
        if step.get("validate") == "phone" and not 7 <= len(re.sub(r"\D", "", value)) <= 15:
            await m.answer("Не похоже на номер телефона. Введите, например: +7 999 123-45-67")
            return
        s["ans"][step["id"]] = {"title": step["title"], "sel": [value], "extra": {}}
        await go(m, s, next_of(step, None, s), edit=False)
    else:
        await m.answer("Пожалуйста, выберите вариант кнопкой выше 👆")


# ───────────────────────── Запуск ─────────────────────────
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(BASE / ".env")
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен от @BotFather.")
    bot = Bot(token)
    dp = Dispatcher()
    dp.include_router(router)
    me = await bot.get_me()
    log.info("Бот запущен: @%s. Остановка — Ctrl+C", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
