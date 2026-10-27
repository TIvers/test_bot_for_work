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
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           Message, ReplyKeyboardMarkup)
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


def is_manager(uid):
    """Менеджер: тот, чей личный чат указан для заявок, либо ID из MANAGER_IDS в .env."""
    ids = {x for x in re.split(r"[,\s]+", os.getenv("MANAGER_IDS", "")) if x}
    mc = manager_chat()
    return str(uid) in ids or (mc is not None and str(mc) == str(uid))


# ───────────────────────── Стартовые кнопки (внизу экрана) ─────────────────────────
BTN_MENU, BTN_CONTACTS = "🏠 Главное меню", "📞 Контакты"
BTN_ADMIN, BTN_LEADS = "🔧 Админ-панель", "📋 Последние заявки"
REPLY_BUTTONS = {BTN_MENU, BTN_CONTACTS, BTN_ADMIN, BTN_LEADS}


def reply_kb(uid):
    rows = [[KeyboardButton(text=BTN_MENU), KeyboardButton(text=BTN_CONTACTS)]]
    extra = []
    if is_manager(uid) or is_admin(uid):
        extra.append(KeyboardButton(text=BTN_LEADS))
    if is_admin(uid):
        extra.append(KeyboardButton(text=BTN_ADMIN))
    if extra:
        rows.append(extra)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


async def refresh_kb(m: Message):
    """Клавиатура привязана к сообщению: отправляем его и сразу удаляем, кнопки остаются."""
    carrier = await m.answer("⌨️", reply_markup=reply_kb(m.from_user.id))
    await try_delete(carrier)


def leads_text(limit=5):
    try:
        with LEADS_FILE.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f, delimiter=";"))[1:]
    except FileNotFoundError:
        rows = []
    if not rows:
        return "📋 Заявок пока нет."
    out = [f"📋 Последние заявки (всего {len(rows)}):"]
    for r in reversed(rows[-limit:]):
        date, name, phone, _, when, tg, flow, est = (r + [""] * 8)[:8]
        out.append(f"\n{date} · {name}\n📞 {phone} · {tg}\n📌 {flow}" + (f"\n💰 {est}" if est else ""))
    return "\n".join(out)[:3900]


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


async def render(msg: Message, s, text, markup):
    """Единственное меню: редактируем сохранённое сообщение, новое шлём только если старого нет."""
    mid = s.get("menu")
    if mid:
        try:
            await msg.bot.edit_message_text(text, chat_id=msg.chat.id, message_id=mid, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    sent = await msg.bot.send_message(msg.chat.id, text, reply_markup=markup)
    s["menu"] = sent.message_id


async def show(msg: Message, s, sid, notice=""):
    step = STEPS[sid]
    text = step_text(step).replace("{name}", client_name(s))
    if notice:
        text = f"{notice}\n\n{text}"
    await render(msg, s, text, keyboard(step, s))


async def prompt(msg: Message, s, text):
    """Вопрос «Напишите…» показываем в том же меню, с кнопкой отмены."""
    await render(msg, s, text, InlineKeyboardMarkup(inline_keyboard=[btn(BACK, "cancelask")]))


async def try_delete(m: Message):
    try:
        await m.delete()
    except TelegramBadRequest:
        pass


async def go(msg: Message, s, sid, notice=""):
    if sid == "main":                   # главное меню = начать заявку заново
        s.update(stack=["main"], ans={}, files=[], ask=None, submitted=False)
    else:
        push(s, sid)
    if sid == "f_done" and not s["submitted"]:
        s["submitted"] = True
        await submit_lead(msg.bot, s)
    await show(msg, s, sid, notice)


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
    await try_delete(m)
    old = sessions.pop(m.from_user.id, None)
    if old and old.get("menu"):         # убираем прежнее меню, чтобы осталось одно
        try:
            await m.bot.delete_message(m.chat.id, old["menu"])
        except TelegramBadRequest:
            pass
    s = sess(m.from_user)
    await refresh_kb(m)
    await go(m, s, "privacy")


@router.message(Command("menu"))
async def cmd_menu(m: Message):
    await try_delete(m)
    await go(m, sess(m.from_user), "main")


@router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"Ваш Telegram ID: {m.from_user.id}\nID этого чата: {m.chat.id}")


# ───────────────────────── Админ-панель ─────────────────────────
def admin_home():
    return ("🔧 Панель администратора", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Тексты сообщений", callback_data="adm:groups")],
        [InlineKeyboardButton(text="📬 Куда приходят заявки", callback_data="adm:mgr")],
        [InlineKeyboardButton(text="✖️ Закрыть", callback_data="adm:exit")]]))


async def open_admin(m: Message, s):
    s["ask"] = None
    text, markup = admin_home()
    await render(m, s, text, markup)        # панель открывается в том же единственном меню


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id):
        await m.answer(f"Нет доступа. Ваш ID: {m.from_user.id}.\nЧтобы стать администратором, впишите "
                       "этот ID в ADMIN_IDS в файле .env и перезапустите бота.")
        return
    await try_delete(m)
    await open_admin(m, sess(m.from_user))


@router.message(Command("setmanager"))
async def cmd_setmanager(m: Message):
    if not is_admin(m.from_user.id):
        return
    settings["manager_chat"] = m.chat.id
    save_json(SETTINGS_FILE, settings)
    await m.answer("✅ Готово. Новые заявки будут приходить в этот чат.")
    await refresh_kb(m)                     # обновляем кнопки (появятся кнопки менеджера)


@router.message(F.text.in_(REPLY_BUTTONS))
async def on_reply_button(m: Message):
    await try_delete(m)
    s, t, uid = sess(m.from_user), m.text, m.from_user.id
    s["ask"] = None
    if t == BTN_ADMIN:
        if is_admin(uid):
            await open_admin(m, s)
    elif t == BTN_LEADS:
        if is_admin(uid) or is_manager(uid):
            await render(m, s, leads_text(), InlineKeyboardMarkup(inline_keyboard=[btn("✖️ Закрыть", "adm:exit")]))
    elif t == BTN_CONTACTS:
        if not s["stack"]:
            s["stack"] = ["main"]
        await go(m, s, "contacts")
    else:
        await go(m, s, "main")


def btn(text, data):
    return [InlineKeyboardButton(text=text, callback_data=data)]


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    s = sess(cb.from_user)
    s["menu"] = cb.message.message_id
    _, act, *rest = cb.data.split(":", 2)
    arg = rest[0] if rest else ""
    groups = list(GROUPS)
    s["adm_edit"] = None
    if act == "exit":                       # закрыть панель: вернуться на прежний экран
        if s["stack"]:
            await show(cb.message, s, s["stack"][-1])
        else:
            await go(cb.message, s, "main")
        await cb.answer()
        return
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
    s["menu"] = cb.message.message_id
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
    ask, s["ask"] = s["ask"], None
    cur = STEPS[s["stack"][-1]]
    if ask and cur["type"] == "multi":      # отмена ввода в мульти-выборе снимает отметку
        a = s["ans"].get(ask["step"])
        if a:
            label = cur["options"][ask["opt"]]["label"]
            if label in a["sel"]:
                a["sel"].remove(label)
            a["extra"].pop(label, None)
    await show(cb.message, s, cur["id"])
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
        if s["ask"]:
            await prompt(cb.message, s, opt["ask"])
        else:
            await show(cb.message, s, sid)
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
        await prompt(cb.message, s, opt["ask"])
        await cb.answer()
        return
    await go(cb.message, s, next_of(step, opt, s), notice=opt["say"] or "")
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
        await try_delete(m)
        await render(m, s, "✅ Текст сохранён. Бот уже использует новый вариант.",
                     InlineKeyboardMarkup(inline_keyboard=[btn("🔧 К админ-панели", "adm:home")]))
        return

    if not s["stack"]:
        await cmd_start(m)
        return
    step = STEPS[s["stack"][-1]]
    value = extract(m, s)                           # file_id сохраняется до удаления сообщения
    await try_delete(m)                             # сообщения клиента не остаются в чате
    if value is None:
        return

    if s["ask"]:                                    # клиент отвечает на «Напишите…»
        st = STEPS[s["ask"]["step"]]
        opt = st["options"][s["ask"]["opt"]]
        a = s["ans"].setdefault(st["id"], {"title": st["title"], "sel": [], "extra": {}})
        a["extra"][opt["label"]] = value
        s["ask"] = None
        if st["type"] == "multi":
            await show(m, s, st["id"])              # возвращаем то же меню с отметками
        else:
            await go(m, s, next_of(st, opt, s))
    elif step.get("accept_text"):
        if step.get("validate") == "phone" and not 7 <= len(re.sub(r"\D", "", value)) <= 15:
            await show(m, s, step["id"], notice="⚠️ Не похоже на номер телефона. Введите, например: +7 999 123-45-67")
            return
        s["ans"][step["id"]] = {"title": step["title"], "sel": [value], "extra": {}}
        await go(m, s, next_of(step, None, s))


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
