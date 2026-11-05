"""Telegram front-end for the same agent (long polling, runs in a background thread)."""
import html
import itertools
import os
import re
import threading
import time
from collections import OrderedDict, defaultdict, deque

import httpx

import agent

TXT = {
    "ru": {
        "hello": "👋 <b>Здравствуйте!</b> Я аналитик сделок товарно-сырьевой биржи.\n\n"
                 "Считаю объёмы, цены, покупателей, продавцов и брокеров по реестру за 2022–2026 годы "
                 "(500 000 сделок) и показываю, как получена каждая цифра. Если в реестре ответа нет, скажу прямо.\n\n"
                 "Пишите как удобно: по-русски или по-узбекски, с опечатками тоже пойму.\n\n"
                 "<b>Попробуйте 👇</b>",
        "examples": ["📦 Объём торгов хлопком-сырцом в 2026", "⛽ Цена бензина АИ-92 по месяцам 2025",
                     "🏆 Топ-5 покупателей карбамида", "🧾 Расскажи о сделке № 400000",
                     "❌ Сколько сделок аннулировано в 2024?"],
        "switch": "🇺🇿 Oʻzbekcha", "status": "⏳ Принял вопрос, считаю по реестру…",
        "search": "🔎 Ищу: «{}»", "found": "✅ Нашёл: {}", "sql": "📊 {}", "sql0": "Считаю по реестру",
        "rows": "строк", "write": "✍️ Пишу ответ…", "how": "🧾 Как посчитано", "new": "🆕 Начнём заново. Задайте вопрос.",
        "verified": "✓ Проверено по реестру: {} SQL · {} с", "busy": "⏳ Слишком много вопросов, подождите пару минут.",
        "fail": "⚠️ Не получилось ответить. Попробуйте ещё раз.", "nohow": "Для этого ответа запросы не понадобились.",
        "howh": "<b>Как посчитан ответ</b>",
    },
    "uz": {
        "hello": "👋 <b>Assalomu alaykum!</b> Men tovar-xomashyo birjasi bitimlari tahlilchisiman.\n\n"
                 "2022–2026-yillar reyestri (500 000 ta bitim) boʻyicha hajm, narx, xaridor, sotuvchi va brokerlarni "
                 "hisoblab beraman hamda har bir raqam qanday olinganini koʻrsataman. Reyestrda javob boʻlmasa, ochiq aytaman.\n\n"
                 "Qanday qulay boʻlsa, shunday yozing: oʻzbekcha yoki ruscha, lotin yoki kirillda, xatolar bilan ham tushunaman.\n\n"
                 "<b>Sinab koʻring 👇</b>",
        "examples": ["📦 2026-yilda paxta xomashyosi savdosi hajmi", "⛽ 2025-yilda AI-92 benzini narxi oylar boʻyicha",
                     "🏆 Karbamidning eng yirik 5 ta xaridori", "🧾 400000-raqamli bitim haqida",
                     "❌ 2024-yilda nechta bitim bekor qilingan?"],
        "switch": "🇷🇺 Русский", "status": "⏳ Savol qabul qilindi, reyestr boʻyicha hisoblayapman…",
        "search": "🔎 Qidiryapman: «{}»", "found": "✅ Topildi: {}", "sql": "📊 {}", "sql0": "Reyestr boʻyicha hisoblayapman",
        "rows": "qator", "write": "✍️ Javob yozyapman…", "how": "🧾 Qanday hisoblandi", "new": "🆕 Yangidan boshlaymiz. Savol bering.",
        "verified": "✓ Reyestr boʻyicha tekshirildi: {} ta SQL · {} s", "busy": "⏳ Savollar juda koʻp, bir-ikki daqiqa kuting.",
        "fail": "⚠️ Javob berib boʻlmadi. Qayta urinib koʻring.", "nohow": "Bu javob uchun soʻrovlar kerak boʻlmadi.",
        "howh": "<b>Javob qanday hisoblandi</b>",
    },
}


def detect_lang(text: str, fallback: str) -> str:
    if re.search(r"[ўқғҳЎҚҒҲ]", text):
        return "uz"
    if re.search(r"[а-яА-ЯёЁ]", text):
        return "ru"
    if re.search(r"(oʻ|o‘|o'|gʻ|g‘|ş|ç|ö|ğ|\b(qancha|nechta|necha|qaysi|bitim|narx|yil|bo.?yicha|haqida|savdo|kim\b|"
                 r"sotil|sotuvchi|xarid|qilin|bo.?lgan|eng\b|uchun|va\b))", text, re.I):
        return "uz"
    if re.search(r"\b(skolko|kakoi|kakaya|sdel|cena|tsena|pokupat)", text, re.I):
        return "ru"
    return fallback


def md_to_tg(md: str) -> str:
    """Markdown → Telegram HTML. Tables become aligned <pre> blocks."""
    out, table = [], []

    def flush():
        if not table:
            return
        rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in table
                if not re.match(r"^\s*\|?\s*:?-{2,}", r)]
        rows = [[re.sub(r"\*\*|__|`", "", c) for c in r] for r in rows]
        table.clear()
        if not rows:
            return
        head, body = rows[0], rows[1:]
        ncol = max(map(len, rows))
        w = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(ncol)]
        if sum(w) + 2 * (ncol - 1) <= 32 or not body:  # narrow: keep a real table
            lines = ["  ".join((r[i] if i < len(r) else "").ljust(w[i]) if i == 0 else
                               (r[i] if i < len(r) else "").rjust(w[i]) for i in range(ncol)) for r in rows]
            out.append("<pre>" + html.escape("\n".join(lines)) + "</pre>")
            return
        numbered = head and head[0].strip() in ("#", "№", "N", "No", "Nº")
        for r in body:
            cells = r + [""] * (ncol - len(r))
            if numbered:
                title, rest = f"{cells[0]}. {cells[1]}", list(zip(head[2:], cells[2:]))
            else:
                title, rest = cells[0], list(zip(head[1:], cells[1:]))
            block = [f"<b>{html.escape(title)}</b>"]
            block += [f"   {html.escape(h)}: {html.escape(v)}" for h, v in rest if v]
            out.append("\n".join(block))
            out.append("")

    for line in md.splitlines():
        if line.strip().startswith("|"):
            table.append(line)
            continue
        flush()
        s = html.escape(line)
        s = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<![\w*])[_*](?!\s)(.+?)(?<!\s)[_*](?![\w*])", r"<i>\1</i>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"^\s*[-*]\s+", "• ", s)
        out.append(s)
    flush()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


class Bot:
    def __init__(self, token: str):
        self.api = f"https://api.telegram.org/bot{token}/"
        self.http = httpx.Client(timeout=70)
        self.hist = defaultdict(lambda: deque(maxlen=6))
        self.lang = {}
        self.rate = defaultdict(deque)
        self.store = OrderedDict()  # short id -> payload for inline buttons (callback_data ≤ 64 bytes)
        self.ids = itertools.count(1)
        self.lock = threading.Lock()

    def call(self, method, **kw):
        try:
            return self.http.post(self.api + method, json=kw).json()
        except Exception:  # noqa: BLE001
            return {}

    def keep(self, payload) -> str:
        with self.lock:
            k = str(next(self.ids))
            self.store[k] = payload
            while len(self.store) > 5000:
                self.store.popitem(last=False)
        return k

    def send(self, chat, text, markup=None, edit_id=None):
        parts = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
        for n, part in enumerate(parts):
            kw = dict(chat_id=chat, text=part, parse_mode="HTML", disable_web_page_preview=True)
            if n == len(parts) - 1 and markup:
                kw["reply_markup"] = markup
            method = "editMessageText" if edit_id and n == 0 else "sendMessage"
            if method == "editMessageText":
                kw["message_id"] = edit_id
            r = self.call(method, **kw)
            if not r.get("ok"):  # HTML parse problem → plain text
                kw["text"] = re.sub(r"<[^>]+>", "", part)
                kw.pop("parse_mode")
                r = self.call(method, **kw)
                if not r.get("ok") and method == "editMessageText":
                    kw.pop("message_id")
                    self.call("sendMessage", **kw)

    def welcome(self, chat, lang):
        t = TXT[lang]
        rows = [[{"text": e, "callback_data": "q:" + self.keep(re.sub(r"^\W+\s*", "", e))}] for e in t["examples"]]
        rows.append([{"text": t["switch"], "callback_data": "lang:" + ("ru" if lang == "uz" else "uz")}])
        self.send(chat, t["hello"], {"inline_keyboard": rows})

    def answer(self, chat, text, user_lang):
        lang = self.lang[chat] = detect_lang(text, self.lang.get(chat, user_lang))
        t = TXT[lang]
        q = self.rate[chat]
        now = time.time()
        while q and now - q[0] > 600:
            q.popleft()
        if len(q) >= 60:
            return self.send(chat, t["busy"])
        q.append(now)
        st = self.call("sendMessage", chat_id=chat, text=t["status"])
        sid = st.get("result", {}).get("message_id")
        lines, last_edit, last_typing = [], 0.0, 0.0

        def status():
            nonlocal last_edit
            if sid and time.time() - last_edit > 1.0:
                self.call("editMessageText", chat_id=chat, message_id=sid, text="\n".join(lines[-6:] + [t["write"]]))
                last_edit = time.time()

        answer, followups, evidence, secs = "", [], [], 0
        try:
            for ev in agent.run_agent(text, list(self.hist[chat]), lang):
                if time.time() - last_typing > 4:
                    self.call("sendChatAction", chat_id=chat, action="typing")
                    last_typing = time.time()
                if ev["type"] == "step":
                    if ev["tool"] == "search_entities":
                        lines.append(t["search"].format(ev["input"].get("query", "")))
                    else:
                        lines.append(t["sql"].format(ev["input"].get("purpose") or t["sql0"]))
                    status()
                elif ev["type"] == "step_result":
                    r = ev["result"]
                    if ev["tool"] == "search_entities" and r.get("matches"):
                        m = r["matches"][0]
                        lines[-1] = t["found"].format(m.get("product_family") or m.get("name"))
                    elif "row_count" in r:
                        lines[-1] = lines[-1].replace("📊", "✅", 1) + f" ({r['row_count']} {t['rows']})"
                    status()
                elif ev["type"] == "done":
                    answer, followups, evidence, secs = ev["answer"], ev.get("followups", []), ev["evidence"], ev["seconds"]
                elif ev["type"] == "error":
                    answer = t["fail"]
        except Exception:  # noqa: BLE001
            answer = t["fail"]
        if not answer.strip():
            answer = t["fail"]
        self.hist[chat].extend([{"role": "user", "content": text}, {"role": "assistant", "content": answer}])
        n_sql = sum(1 for e in evidence if e["tool"] == "run_sql" and "error" not in e["result"])
        foot = f"\n\n<i>{t['verified'].format(n_sql, str(secs).replace('.', ','))}</i>" if n_sql else ""
        rows = [[{"text": "💬 " + f, "callback_data": "q:" + self.keep(f)}] for f in followups]
        if evidence:
            rows.append([{"text": t["how"], "callback_data": "how:" + self.keep((lang, evidence))}])
        self.send(chat, md_to_tg(answer) + foot, {"inline_keyboard": rows} if rows else None, edit_id=sid)

    def show_how(self, chat, payload):
        lang, evidence = payload
        t = TXT[lang]
        parts = [t["howh"]]
        for i, e in enumerate(evidence, 1):
            if e["tool"] == "run_sql":
                r = e["result"]
                res = (f"→ {r.get('row_count')} {t['rows']}, {r.get('ms')} ms" if "row_count" in r
                       else "→ " + str(r.get("error", ""))[:200])
                parts.append(f"\n<b>{i}.</b> {html.escape(e['input'].get('purpose') or 'SQL')}\n"
                             f"<pre>{html.escape(e['input'].get('sql', '')[:1500])}</pre>{html.escape(res)}")
            else:
                ms = ", ".join(html.escape(str(m.get("product_family") or m.get("name"))) for m in
                               e["result"].get("matches", [])[:3])
                parts.append(f"\n<b>{i}.</b> 🔎 «{html.escape(e['input'].get('query', ''))}» → {ms or '—'}")
        self.send(chat, "\n".join(parts) if evidence else t["nohow"])

    def handle_message(self, msg):
        chat = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        ulang = "uz" if (msg.get("from", {}).get("language_code") or "").startswith("uz") else "ru"
        if not text:
            return
        if text.startswith(("/start", "/help")):
            self.lang.setdefault(chat, ulang)
            return self.welcome(chat, self.lang[chat])
        if text.startswith("/new"):
            self.hist[chat].clear()
            return self.send(chat, TXT[self.lang.get(chat, ulang)]["new"])
        self.answer(chat, text, ulang)

    def handle_callback(self, cb):
        self.call("answerCallbackQuery", callback_query_id=cb["id"])
        chat = cb["message"]["chat"]["id"]
        kind, _, key = (cb.get("data") or "").partition(":")
        if kind == "lang" and key in TXT:
            self.lang[chat] = key
            return self.welcome(chat, key)
        payload = self.store.get(key)
        if payload is None:
            return
        if kind == "q":
            self.send(chat, f"<b>❓ {html.escape(payload)}</b>")
            self.answer(chat, payload, self.lang.get(chat, "ru"))
        elif kind == "how":
            self.show_how(chat, payload)

    def loop(self):
        self.call("deleteWebhook")
        self.call("setMyCommands", commands=[{"command": "start", "description": "Примеры вопросов / Misollar"},
                                             {"command": "new", "description": "Новый диалог / Yangi suhbat"}])
        self.call("setMyDescription", description="Аналитик сделок биржи: объёмы, цены, участники и брокеры за "
                                                  "2022–2026. Отвечаю на русском и узбекском, каждая цифра — из реестра.\n\n"
                                                  "Birja bitimlari tahlilchisi: hajm, narx, ishtirokchi va brokerlar.")
        offset = 0
        while True:
            r = self.call("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                if "message" in u:
                    threading.Thread(target=self.handle_message, args=(u["message"],), daemon=True).start()
                elif "callback_query" in u:
                    threading.Thread(target=self.handle_callback, args=(u["callback_query"],), daemon=True).start()
            if not r.get("ok"):
                time.sleep(3)


def start_background():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        threading.Thread(target=Bot(token).loop, daemon=True, name="tgbot").start()
        return True
    return False


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    Bot(os.environ["TELEGRAM_BOT_TOKEN"]).loop()
