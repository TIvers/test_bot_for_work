# -*- coding: utf-8 -*-
"Telegram-бот @test2_for_work_bot"
import asyncio
import csv
import json
import logging
import os
import re
from datetime import datetime
from io import BytesIO
from fnmatch import fnmatch
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, BotCommand, BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery,
                           FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands,
                           Message, ReplyKeyboardRemove)
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


# ───────────────────────── Меню бота (кнопка «Меню» рядом с полем ввода) ─────────────────────────
def menu_commands(uid, in_branch):
    cmds = [BotCommand(command="start", description="Вернуться к началу" if in_branch else "Начать"),
            BotCommand(command="contacts", description="Контакты")]
    if is_manager(uid) or is_admin(uid):
        cmds.append(BotCommand(command="leads", description="Последние заявки"))
    if is_admin(uid):
        cmds.append(BotCommand(command="admin", description="Админ-панель"))
    return cmds


async def update_menu(bot: Bot, chat_id, s, force=False):
    """«Начать» → «Вернуться к началу», как только клиент ушёл в какую-либо ветку."""
    uid, st = s["user"]["id"], s["stack"]
    in_branch = bool(st) and st[-1] not in ("privacy", "main")
    key = (in_branch, is_manager(uid), is_admin(uid))
    if not force and s.get("menu_key") == key:
        return
    try:
        await bot.set_my_commands(menu_commands(uid, in_branch), scope=BotCommandScopeChat(chat_id=chat_id))
        s["menu_key"] = key
    except Exception:
        log.exception("Не удалось обновить меню команд")


async def clear_kb(m: Message):
    """Убираем старую нижнюю клавиатуру (если осталась с прошлой версии бота)."""
    carrier = await m.answer("⌨️", reply_markup=ReplyKeyboardRemove(), disable_notification=True)
    await try_delete(carrier)


# ───────────────────────── Заявки: хранилище ─────────────────────────
LEAD_COLS = ["ID", "Дата", "Имя", "Телефон", "Мессенджеры", "Время", "Telegram", "Ветка", "Оценка", "Ответы"]
LEADS_PAGE = 5


def write_leads(leads):
    with LEADS_FILE.open("w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig — чтобы Excel не ломал кириллицу
        w = csv.writer(f, delimiter=";")
        w.writerow(LEAD_COLS)
        for l in leads:
            w.writerow([l[c] for c in LEAD_COLS])


def read_leads():
    try:
        with LEADS_FILE.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f, delimiter=";"))
    except FileNotFoundError:
        return []
    if not rows:
        return []
    head, body = rows[0], rows[1:]
    old_format = not head or head[0] != "ID"
    if old_format:                                  # файл со старой версии бота: нумеруем по порядку
        body = [[str(i)] + r for i, r in enumerate(body, 1)]
    leads = [dict(zip(LEAD_COLS, (r + [""] * len(LEAD_COLS))[:len(LEAD_COLS)])) for r in body]
    if old_format:
        write_leads(leads)
        settings["last_lead_id"] = max([int(l["ID"]) for l in leads] + [settings.get("last_lead_id", 0)])
        save_json(SETTINGS_FILE, settings)
    return leads


def next_lead_id(leads):
    """ID растёт всегда: номер удалённой заявки не выдаётся повторно."""
    nid = max([int(l["ID"]) for l in leads if l["ID"].isdigit()] + [settings.get("last_lead_id", 0)]) + 1
    settings["last_lead_id"] = nid
    save_json(SETTINGS_FILE, settings)
    return nid


def lead_card(l):
    out = [f"🆔 Заявка №{l['ID']} · {l['Дата']}", "", f"👤 {l['Имя']}", f"📞 {l['Телефон']}",
           f"💬 Мессенджеры: {l['Мессенджеры'] or '—'}", f"🕐 Удобное время: {l['Время'] or '—'}",
           f"✈️ Telegram: {l['Telegram']}", f"📌 Ветка: {l['Ветка']}"]
    if l["Оценка"]:
        out.append(f"💰 Вилка: {l['Оценка']}")
    if l["Ответы"]:
        out += ["", "Ответы клиента:"] + l["Ответы"].split(" | ")
    return "\n".join(out)[:3900]


def build_xlsx(leads) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Заявки"
    ws.append(LEAD_COLS)
    for l in leads:
        ws.append([int(l["ID"]) if l["ID"].isdigit() else l["ID"]]
                  + [l[c] for c in LEAD_COLS[1:-1]] + [l["Ответы"].replace(" | ", "\n")])
    for i, w in enumerate([6, 17, 20, 18, 22, 18, 18, 28, 22, 70]):
        ws.column_dimensions[chr(65 + i)].width = w
    wrap = Alignment(wrap_text=True, vertical="top")
    for row in ws.iter_rows():
        for c in row:
            c.alignment = wrap
            if isinstance(c.value, str):
                c.data_type = "s"                   # текст клиента не должен выполняться как формула
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


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
    s["ld_ask"] = False
    await render(msg, s, text, keyboard(step, s))
    await update_menu(msg.bot, msg.chat.id, s)


async def prompt(msg: Message, s, text):
    """Вопрос «Напишите…» показываем в том же меню, с кнопкой отмены."""
    await render(msg, s, text, InlineKeyboardMarkup(inline_keyboard=[btn(BACK, "cancelask")]))


async def try_delete(m: Message):
    try:
        await m.delete()
    except TelegramBadRequest:
        pass


TEMP_TTL = 15                           # через сколько секунд исчезают служебные сообщения
_bg: set = set()


async def _delete_later(*msgs: Message, delay=TEMP_TTL):
    await asyncio.sleep(delay)
    for x in msgs:
        await try_delete(x)


async def answer_temp(m: Message, text, delay=TEMP_TTL):
    """Служебное сообщение: показываем, а через `delay` секунд удаляем вместе с командой клиента."""
    sent = await m.answer(text)
    task = asyncio.create_task(_delete_later(m, sent, delay=delay))
    _bg.add(task)
    task.add_done_callback(_bg.discard)
    return sent


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

    leads = read_leads()
    lead_id = next_lead_id(leads)
    card = (f"🔔 Новая заявка №{lead_id}\n\n👤 {name}\n📞 {phone}\n💬 Мессенджеры: {messengers or '—'}\n"
            f"🕐 Удобное время: {when or '—'}\n✈️ Telegram: {tg}\n📌 Ветка: {flow}\n")
    if est:
        card += f"💰 Предварительная вилка: {est} (ориентир, не оферта)\n"
    card += "\nОтветы клиента:\n" + "\n".join(lines)

    leads.append(dict(zip(LEAD_COLS, [str(lead_id), datetime.now().strftime("%Y-%m-%d %H:%M"), name, phone,
                                      messengers, when, tg, flow, est or "", " | ".join(lines)])))
    write_leads(leads)

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
    await go(m, s, "privacy")
    await clear_kb(m)


@router.message(Command("menu"))
async def cmd_menu(m: Message):
    await try_delete(m)
    await go(m, sess(m.from_user), "main")


@router.message(Command("myid"))
async def cmd_myid(m: Message):
    await answer_temp(m, f"Ваш Telegram ID: {m.from_user.id}\nID этого чата: {m.chat.id}", delay=60)


# ───────────────────────── Админ-панель ─────────────────────────
def admin_home():
    return ("🔧 Панель администратора", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Тексты сообщений", callback_data="adm:groups")],
        [InlineKeyboardButton(text="📋 Заявки", callback_data="ld:l:0")],
        [InlineKeyboardButton(text="📬 Куда приходят заявки", callback_data="adm:mgr")],
        [InlineKeyboardButton(text="✖️ Закрыть", callback_data="adm:exit")]]))


async def open_admin(m: Message, s):
    s["ask"] = None
    s["ld_ask"] = False
    text, markup = admin_home()
    await render(m, s, text, markup)        # панель открывается в том же единственном меню


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id):
        await answer_temp(m, f"Нет доступа. Ваш ID: {m.from_user.id}.\nЧтобы стать администратором, впишите "
                             "этот ID в ADMIN_IDS в файле .env и перезапустите бота.", delay=60)
        return
    await try_delete(m)
    await open_admin(m, sess(m.from_user))


@router.message(Command("setmanager"))
async def cmd_setmanager(m: Message):
    if not is_admin(m.from_user.id):
        return
    settings["manager_chat"] = m.chat.id
    save_json(SETTINGS_FILE, settings)
    await answer_temp(m, "✅ Готово. Новые заявки будут приходить в этот чат.")
    await update_menu(m.bot, m.chat.id, sess(m.from_user), force=True)   # в меню появится «Последние заявки»


@router.message(Command("contacts"))
async def cmd_contacts(m: Message):
    await try_delete(m)
    s = sess(m.from_user)
    s["ask"] = None
    if not s["stack"]:
        s["stack"] = ["main"]
    await go(m, s, "contacts")


@router.message(Command("leads"))
async def cmd_leads(m: Message):
    if not (is_admin(m.from_user.id) or is_manager(m.from_user.id)):
        return
    await try_delete(m)
    s = sess(m.from_user)
    s["ask"] = None
    await show_leads(m, s, 0)


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


# ───────────────────────── Меню заявок ─────────────────────────
def leads_screen(page, notice=""):
    leads = read_leads()[::-1]                      # свежие сверху
    pages = max(1, -(-len(leads) // LEADS_PAGE))
    page = min(max(page, 0), pages - 1)
    if leads:
        text = (f"📋 Заявки: всего {len(leads)}, стр. {page + 1}/{pages}\n"
                "Нажмите на заявку, чтобы открыть подробности.")
    else:
        text = "📋 Заявок пока нет."
    if notice:
        text = f"{notice}\n\n{text}"
    rows = [btn(f"№{l['ID']} · {l['Дата'][8:10]}.{l['Дата'][5:7]} · {l['Имя'][:16]} · {l['Ветка'][:22]}",
                f"ld:v:{l['ID']}") for l in leads[page * LEADS_PAGE:(page + 1) * LEADS_PAGE]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"ld:l:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"ld:l:{page + 1}"))
    if nav:
        rows.append(nav)
    if leads:
        rows += [btn("📥 Скачать все в Excel", "ld:xlsx"), btn("🗑 Удалить по ID", "ld:ask")]
    rows.append(btn("✖️ Закрыть", "ld:close"))
    return text, InlineKeyboardMarkup(inline_keyboard=rows), page


async def show_leads(msg: Message, s, page=0, notice=""):
    s.update(ld_ask=False, ask=None)
    text, markup, s["ld_page"] = leads_screen(page, notice)
    await render(msg, s, text, markup)


async def confirm_delete(msg: Message, s, ids, notice=""):
    s["ld_pending"] = ids
    text = f"Удалить заявки {', '.join('№' + i for i in ids)}? Это нельзя отменить."
    if notice:
        text = f"{notice}\n\n{text}"
    await render(msg, s, text, InlineKeyboardMarkup(inline_keyboard=[
        btn("✅ Да, удалить", "ld:dok"), btn("Отмена", f"ld:l:{s.get('ld_page', 0)}")]))


async def ask_lead_ids(msg: Message, s, notice=""):
    s["ld_ask"] = True
    text = ("Отправьте ID заявки одним сообщением (он в первой колонке Excel-файла и в карточке заявки).\n"
            "Можно несколько: 3, 5, 8.")
    if notice:
        text = f"{notice}\n\n{text}"
    await render(msg, s, text, InlineKeyboardMarkup(inline_keyboard=[btn("Отмена", f"ld:l:{s.get('ld_page', 0)}")]))


@router.callback_query(F.data.startswith("ld:"))
async def cb_leads(cb: CallbackQuery):
    uid = cb.from_user.id
    if not (is_admin(uid) or is_manager(uid)):
        await cb.answer("Нет доступа", show_alert=True)
        return
    s = sess(cb.from_user)
    s["menu"] = cb.message.message_id
    s["ld_ask"] = False
    _, act, *rest = cb.data.split(":")
    arg = rest[0] if rest else ""

    if act == "close":
        if s["stack"]:
            await show(cb.message, s, s["stack"][-1])
        else:
            await go(cb.message, s, "main")
    elif act == "l":
        await show_leads(cb.message, s, int(arg))
    elif act == "v":
        lead = next((l for l in read_leads() if l["ID"] == arg), None)
        if not lead:
            await show_leads(cb.message, s, s.get("ld_page", 0), "⚠️ Заявка не найдена (возможно, уже удалена).")
        else:
            await render(cb.message, s, lead_card(lead), InlineKeyboardMarkup(inline_keyboard=[
                btn("🗑 Удалить эту заявку", f"ld:del:{arg}"), btn("◀️ К списку", f"ld:l:{s.get('ld_page', 0)}")]))
    elif act == "del":
        await confirm_delete(cb.message, s, [arg])
    elif act == "ask":
        await ask_lead_ids(cb.message, s)
    elif act == "dok":
        ids = set(s.pop("ld_pending", []))
        leads = read_leads()
        keep = [l for l in leads if l["ID"] not in ids]
        write_leads(keep)
        await show_leads(cb.message, s, s.get("ld_page", 0), f"🗑 Удалено заявок: {len(leads) - len(keep)}.")
    elif act == "xlsx":
        leads = read_leads()
        if not leads:
            await cb.answer("Заявок пока нет", show_alert=True)
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        try:
            doc = BufferedInputFile(build_xlsx(leads), filename=f"leads_{stamp}.xlsx")
            await cb.message.answer_document(doc, caption=f"📥 Все заявки ({len(leads)} шт.)")
        except ImportError:
            await cb.message.answer_document(FSInputFile(LEADS_FILE, filename=f"leads_{stamp}.csv"),
                                             caption="Библиотека openpyxl не установлена (pip install openpyxl), "
                                                     "поэтому отправлен CSV — он тоже открывается в Excel.")
        await cb.answer("Файл отправлен")
        return
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

    if s.get("ld_ask") and m.text and (is_admin(m.from_user.id) or is_manager(m.from_user.id)):
        await try_delete(m)                         # администратор вводит ID заявок для удаления
        ids = list(dict.fromkeys(str(int(x)) for x in re.findall(r"\d+", m.text)))
        have = {l["ID"] for l in read_leads()}
        found, missing = [i for i in ids if i in have], [i for i in ids if i not in have]
        note = f"⚠️ Не найдены ID: {', '.join(missing)}." if missing else ""
        if found:
            s["ld_ask"] = False
            await confirm_delete(m, s, found, note)
        else:
            await ask_lead_ids(m, s, note or "⚠️ Не нашёл в сообщении ни одного ID.")
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
    await bot.set_my_commands([BotCommand(command="start", description="Начать"),
                               BotCommand(command="contacts", description="Контакты")],
                              scope=BotCommandScopeDefault())
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    me = await bot.get_me()
    log.info("Бот запущен: @%s. Остановка — Ctrl+C", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
