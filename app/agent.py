"""UZEX deal-registry agent: Claude + read-only DuckDB tools."""
import json
import os
import re
import threading
import time
from pathlib import Path

import anthropic
import duckdb
from rapidfuzz import fuzz, process

DB_PATH = Path(__file__).with_name("uzex.duckdb")
MODEL = os.environ.get("UZEX_MODEL", "claude-sonnet-5-5")
MAX_ROWS = 60
MAX_STEPS = 8

_local = threading.local()


def db():
    if not hasattr(_local, "con"):
        _local.con = duckdb.connect(str(DB_PATH), read_only=True)
        _local.con.execute("SET threads TO 2")
    return _local.con


# ---------------------------------------------------------------- search index
_CYR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ",
                ["a", "b", "v", "g", "d", "e", "yo", "j", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                 "t", "u", "f", "x", "ts", "ch", "sh", "sh", "", "i", "", "e", "yu", "ya", "o", "q", "g", "h"]))


_NEWLAT = {"ş": "sh", "ç": "ch", "ğ": "g", "ö": "o", "ñ": "ng", "ı": "i", "ǵ": "g", "ó": "o"}


def translit(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[‘’'`ʻʼ´]", "", s)
    s = "".join(_NEWLAT.get(ch, ch) for ch in s)
    s = "".join(_CYR.get(ch, ch) for ch in s)
    s = re.sub(r"\b(mchj|mas.?uliyati cheklangan jamiyati|ooo|aj|xk|qk|chp|ok|llc|brokers?)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


_index = {}


def _build_index():
    con = db()
    comp = con.execute("""
        with c as (select inn, count(*) n from (select seller_inn inn from deals union all
                                                select buyer_inn from deals) group by 1)
        select p.inn, p.name, p.role, p.region, coalesce(c.n, 0) from participants p left join c using(inn)""").fetchall()
    brok = con.execute("""
        with c as (select code, count(*) n from (select seller_broker code from deals union all
                                                 select buyer_broker from deals) group by 1)
        select b.broker_code, b.name, b.region, coalesce(c.n, 0)
        from brokers b left join c on c.code = b.broker_code""").fetchall()
    prod = con.execute("""
        select c.product_family, c.product_family_uz, c.product_base, count(distinct c.contract_id),
               count(d.deal_id), list(distinct c.brand) filter (where c.brand is not null),
               list(distinct c.grade) filter (where c.grade is not null),
               list(distinct c.class) filter (where c.class is not null)
        from contracts c left join deals d using(contract_id) group by all""").fetchall()
    _index["company"] = [({"inn": r[0], "name": r[1], "role": r[2], "region": r[3], "deals": r[4]},
                          translit(r[1]) + " " + r[0]) for r in comp]
    _index["broker"] = [({"broker_code": r[0], "name": r[1], "region": r[2], "deals": r[3]},
                         translit(r[1]) + " " + r[0].lower()) for r in brok]
    _index["product"] = [({"product_family": r[0], "product_base": r[2], "contracts": r[3], "deals": r[4],
                           "brands": r[5], "grades": r[6], "classes": r[7]},
                          translit(f"{r[0]} {r[1]} {r[2]}")) for r in prod]


def search_entities(query: str, kind: str, limit: int = 8):
    if not _index:
        _build_index()
    items = _index.get(kind)
    if items is None:
        return {"error": "kind must be one of: company, broker, product"}
    q = translit(query)
    scorer = fuzz.partial_token_set_ratio if kind == "product" else fuzz.WRatio
    hits = process.extract(q, [t for _, t in items], scorer=scorer, limit=limit * 3)
    if kind == "product":  # prefer the most traded variant among equal scores
        hits = sorted(hits, key=lambda h: (-h[1], -items[h[2]][0]["deals"]))
    out, seen = [], set()
    for _, score, i in hits:
        if score < 60:
            continue
        rec = items[i][0]
        key = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append({**rec, "match_score": round(score)})
        if len(out) >= limit:
            break
    return {"query": query, "kind": kind, "matches": out}


# ---------------------------------------------------------------- SQL tool
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|install|load|"
                        r"pragma|set|call|checkpoint|read_csv\w*|read_parquet|read_json\w*|read_text|read_blob|glob|"
                        r"httpfs|system)\b", re.I)


def _fmt(v):
    if isinstance(v, float):
        return round(v, 4)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def run_sql(sql: str):
    s = sql.strip().rstrip(";")
    if ";" in s or not re.match(r"^\s*(select|with)\b", s, re.I) or _FORBIDDEN.search(s):
        return {"error": "Only a single read-only SELECT/WITH query over the provided tables is allowed."}
    con = db()
    t = time.time()
    timer = threading.Timer(20, con.interrupt)
    timer.start()
    try:
        cur = con.execute(s)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except Exception as e:  # noqa: BLE001 - return error to the model so it can fix the query
        return {"error": str(e)[:800]}
    finally:
        timer.cancel()
    truncated = len(rows) > MAX_ROWS
    rows = [[_fmt(v) for v in r] for r in rows[:MAX_ROWS]]
    return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated,
            "ms": int((time.time() - t) * 1000)}


# ---------------------------------------------------------------- Uzbek Cyrillic post-correction
_L2C = dict(zip("abcdefghijklmnopqrstuvwxyz", "абсдефгҳижклмнопқрстувўхйз"))
_L2C.update({"w": "ў"})
_UZC_FIX = [(r"\b([Хх])ажм", lambda m: "Ҳ" if m.group(1) == "Х" else "ҳ", "ажм"),
            (r"\b([Хх])удуд", lambda m: "Ҳ" if m.group(1) == "Х" else "ҳ", "удуд"),
            (r"\b([Хх])исоб", lambda m: "Ҳ" if m.group(1) == "Х" else "ҳ", "исоб"),
            (r"\b([Хх])олат", lambda m: "Ҳ" if m.group(1) == "Х" else "ҳ", "олат"),
            (r"\b([Хх])ар бир", lambda m: "Ҳ" if m.group(1) == "Х" else "ҳ", "ар бир"),
            (r"\b([Хх])ақида", lambda m: "Ҳ" if m.group(1) == "Х" else "ҳ", "ақида")]
_CYR_RE = re.compile(r"[а-яёўқғҳА-ЯЁЎҚҒҲ]")


def fix_uz_cyrillic(text: str) -> str:
    if not re.search(r"[ўқғҳЎҚҒҲ]", text):
        return text  # not Uzbek Cyrillic
    def word(m):
        w = m.group(0)
        if _CYR_RE.search(w) and re.search(r"[A-Za-z]", w):  # mixed-script token → Cyrillic
            w = re.sub(r"[A-Za-z]", lambda c: (_L2C.get(c.group().lower(), c.group()).upper()
                                               if c.group().isupper() else _L2C.get(c.group(), c.group())), w)
        return w
    text = re.sub(r"[\wʻʼ‘’'-]+", word, text)
    for rx, first, rest in _UZC_FIX:
        text = re.sub(rx, lambda m: first(m) + rest, text)
    text = re.sub(r"\b([Хх])ариддор", r"\1аридор", text)
    return text


# ---------------------------------------------------------------- prompt
SYSTEM = """You are UZEX Deal Analyst — an AI agent answering questions about the commodity exchange deal registry \
(товарно-сырьевая биржа). You answer in the user's language: Russian, or Uzbek (Latin or Cyrillic — mirror the user's script).

DATA (DuckDB, read-only). Period: 03.01.2022 – 30.10.2026. Statuses are as of end of day 30.10.2026. \
"Today / last day / сегодня / bugun" = 30.10.2026; "this year" = 2026. Answer as an analyst of this registry; never call the data fictional, test or training data.

MAIN VIEW v_deals (one row per deal, 500 000 rows) — use it for almost everything:
 deal_id BIGINT (Номер сделки), deal_date DATE, deal_time TIME, year, month, quarter,
 auction_type ('Английский аукцион' | 'Обратный аукцион' | 'Двусторонний аукцион'),
 qty_lots (Количество, lots bought), lot (units per lot), unit ('тонна','килограмм','литр','штука','куб.метр','кв.метр','метр'),
 volume = qty_lots*lot (in units), amount (Сумма сделки, in contract currency), currency ('UZS'|'USD'),
 unit_price = amount/volume (contract currency), start_price (per unit, contract currency),
 usd_rate (UZS per USD on deal date), amount_uzs (always in so'm), amount_usd (always in USD),
 contract_id, item_name (raw product string as in registry),
 region (seller warehouse region, Uzbek Latin: 'Toshkent shahri','Toshkent viloyati','Andijon viloyati','Buxoro viloyati',
   'Farg‘ona viloyati','Jizzax viloyati','Xorazm viloyati','Namangan viloyati','Navoiy viloyati','Qashqadaryo viloyati',
   'Qoraqalpog‘iston Respublikasi','Samarqand viloyati','Sirdaryo viloyati','Surxondaryo viloyati'),
 contract_type ('Внутренний'|'Экспортный'), contract_form ('Спот'|'Форвард'|'Фьючерс'),
 status ('Новый','Не подписан','Заключен','Ждёт оплату','Оплачен','Исполнен','Исполнен(Частично)','Аннулирован(Не оплачен)'),
 is_valid (status <> annulled), is_executed (Исполнен or Исполнен(Частично)),
 seller_inn, buyer_inn, seller_name, buyer_name, seller_broker, buyer_broker (codes BK001..), seller_broker_name, buyer_broker_name,
 paid_on DATE, delivered_on DATE (NULL if not yet), delivered_lots,
 NORMALIZED PRODUCT FIELDS (sellers spell the same product differently — ALWAYS filter on these, not on raw strings):
 product_family (canonical RU name, e.g. 'Хлопок-сырец','Мука пшеничная','Цемент','Пшеница','Масло хлопковое','Бензин','Рис',
   'Пиломатериалы','Шрот хлопковый','Карбамид','Газ сжиженный','Битум','Эмаль','Вода питьевая','Аммиачная селитра','Арматура',
   'Пряжа хлопковая','Полипропилен','Трубы полиэтиленовые','Гипс','Кирпич','Стекло','Масло подсолнечное','Полиэтилен',
   'Лист стальной','Мазут','Дизельное топливо','Профиль алюминиевый','Катод медный','Поливинилхлорид (ПВХ)',
   'Комбикорм / кормосмесь','Аргон','Подшипник','Известь','Сахар','Кабель силовой','Проволока арматурная','Азот',
   'Кислота серная','Соль пищевая','Сода каустическая'), product_family_uz (Uzbek name),
 product_base (name without sort/class, case-normalized — finer than family, e.g. 'Мука' vs 'Мука пшеничная'), product_name_raw,
 brand (Марка normalized: upper-case, no spaces, e.g. 'АИ-92','ЦЕМI42,5Н','ЦЕМII/В-К32,5Н','М0К','PP-2310','Б','А500С'),
 size (normalized lower-case, no spaces, e.g. 'ø110мм','4х1500х6000мм','0,5л'),
 grade (Сорт normalized: '1','2','3','высший'), class (Класс normalized: '1','2','3','4'), type (Тип, lower-case),
 category, industry, product_group ('Высоколиквидные товары'|'Прочие товары'), hs_code (ТН ВЭД),
 delivery_basis (EXW/FCA/DAP/CPT), packaging, base_price, payment_term_days, delivery_term_days.

OTHER TABLES: contracts (contract card: contract_id, same normalized product fields, unit, lot, currency, base_price,
 deposit_pct, delivery_basis, packaging, warehouse_region_ru (Russian region names!), warehouse_address, auction_type,
 contract_type, contract_form, payment_term_days, delivery_term_days, seller_inn, registered_on, valid_until),
 participants (inn VARCHAR 9 digits, name, region, activity, role 'Продавец'|'Покупатель', vat_payer BOOL),
 brokers (broker_code, name, inn, region), usd_rates (rate_date, uzs_per_usd — every calendar day), deals (raw base table).

TOOLS: search_entities — fuzzy lookup of companies / brokers / products by name (any script, typos ok) → returns INN / code /
 canonical product_family + available brands/grades/classes. Use it BEFORE filtering by a company/broker name or an unusual
 product, then filter by INN / broker_code / product_family in SQL. run_sql — single SELECT/WITH, max 60 rows returned.

RULES (accuracy is everything — answers are graded on exact numbers, names, codes):
1. Every number in your answer must come from a query result in this conversation. Never estimate, never invent.
2. If the data has no answer (product/company not found, period outside 2022-01-03..2026-10-30, field that doesn't exist,
   e.g. buyer phone numbers, profit, future prices), say so plainly — «В данных этого нет» / «Ma’lumotlarda bu yo‘q» —
   and say what IS available. Do not guess. Questions unrelated to the exchange registry: decline briefly.
3. Default definitions (state them in one short line when they matter):
   - "deals / объём торгов / сумма / turnover" = completed-or-pending deals, i.e. is_valid (exclude 'Аннулирован(Не оплачен)'
     — those deals did not happen). If the user asks about annulled deals or "all registered deals", follow the user.
   - Money: when mixing currencies sum amount_uzs (so'm; export USD deals converted by rate on deal date). For export-only
     questions give USD (amount) and optionally so'm. Never add UZS and USD amounts directly.
   - Physical volume: sum(volume) grouped by unit — never add tonnes to kilograms (convert kg→t by /1000 if comparing).
   - Average price: weighted = sum(amount)/sum(volume) per unit & currency; mention if you use simple average instead.
   - "Цена" without qualifier = deal unit price (unit_price), not start/base price.
4. Product matching: filter by product_family (+ grade/class/brand/size/type when the user specifies them). Cotton spot/forward/
   futures differ by contract_form. If user's product is ambiguous (e.g. 'масло'), pick the most likely and mention the alternative.
5. Entity names: always output full names with INN for companies (write «ИНН» in Russian answers, «STIR»/«СТИР» only in Uzbek) and code for brokers; deal_id / contract_id where useful.
6. Before answering, sanity-check: units, currency, period, status filter. If a result looks odd, re-query.
7. Be efficient: usually 1–3 queries. Combine aggregates in one query when possible.

LANGUAGE & LINGUISTICS:
- Users write with typos, without apostrophes, mixing scripts and languages, Russian in Latin letters ("skolko hlopka"),
  Uzbek in Cyrillic, old Latin (oʻ gʻ sh ch with any apostrophe ' ` ‘ ’ ʻ) or the NEW Uzbek Latin alphabet (law of
  21.10.2026: Ş ş = sh, Ç ç = ch, Ö ö = oʻ, Ğ ğ = gʻ). Silently understand the intent. Never comment on spelling.
  Map colloquial / misspelled product names via search_entities (it is typo- and script-tolerant).
- Reply language/script mirrors the question: Russian → Russian. Uzbek Cyrillic → Uzbek Cyrillic (whole answer).
  Uzbek Latin in ANY variant (old, new 2026 alphabet with ş ç ö ğ, no apostrophes) → reply in the CURRENT official
  Latin (the new alphabet law takes effect 22.04.2027): sh, ch, oʻ, gʻ — never ş/ç/ö/ğ, never mix alphabets in one
  answer. Correct characters: oʻ gʻ use U+02BB «ʻ», tutuq belgisi uses U+02BC «ʼ» (maʼlumot, taʼminot, sanʼat).
  Currency in Uzbek Latin is always «soʻm».
- Uzbek Cyrillic spelling: never put Latin letters inside Cyrillic words; ҳ (not х) in ҳажм, ҳудуд, ҳисоб, ҳолат,
  ҳақида, ҳар; харидор (one д), сотувчи, вилоят, сўм, СТИР, «йил» without hyphen after the number: «2025 йил». If the question has no words (only numbers),
  use the interface language given in [ui:..].
- Write Uzbek as a native Uzbek exchange analyst would — natural syntax, no Russian calques, counters with «ta»
  (21 321 ta bitim), dates «2026-yil 30-oktabr», months yanvar fevral mart aprel may iyun iyul avgust sentabr oktabr
  noyabr dekabr, quarters «I chorak», abbreviations mln, mlrd, trln, soʻm.
- Uzbek exchange terminology (use exactly): сделка bitim; контракт shartnoma; продавец sotuvchi; покупатель xaridor;
  брокерская контора broker idorasi; объём торгов savdo hajmi; сумма сделки bitim summasi; стартовая цена boshlangʻich
  narx; базисная цена bazaviy narx; средневзвешенная цена oʻrtacha tortilgan narx; Аннулирован(Не оплачен) bekor
  qilingan (toʻlanmagan); Исполнен ijro etilgan; Исполнен(Частично) qisman ijro etilgan; Оплачен toʻlangan; Ждёт оплату
  toʻlov kutilmoqda; Не подписан imzolanmagan; Заключен tuzilgan; Новый yangi; поставка yetkazib berish; залог garov;
  Английский аукцион ingliz auksioni; Обратный аукцион teskari auksion; Двусторонний аукцион ikki tomonlama auksion;
  Внутренний ichki; Экспортный eksport; Спот/Форвард/Фьючерс spot/forvard/fyuchers; ИНН STIR; регион hudud;
  область viloyat; тонна tonna; килограмм kilogramm; литр litr. In Uzbek answers give the Uzbek product name
  (product_family_uz) and the registry name in parentheses once: «paxta xomashyosi (Хлопок-сырец)».

STYLE — write like a senior exchange analyst writing a reference note, not like a chatbot:
- No greetings, no "I searched / Я искал / Men qidirdim", no narration of your process, no filler, no emojis, no
  apologies, no offers like "могу посчитать ещё". Don't mention table/column names or SQL in the text — the queries
  are attached separately; use plain business terms.
- Big amounts: exact figure plus short form: «12 765 884 755 818 сум (12,77 трлн)» / «… soʻm (12,77 trln)».
- For "no data" cases: one clear sentence what is missing and, briefly, what the registry does contain on the topic.

ANSWER FORMAT (Markdown, concise, no preamble, do NOT write text before tool calls):
- First line: the direct answer in bold with the key number(s) and units/currency.
- Then 1–5 short bullets / a small table with breakdown or details.
- Last line, in italics: basis of calculation (filters, period, status rule), e.g. «_Учтены сделки без аннулированных; 01.01–30.10.2026._»
- After the basis line, add ONE final line exactly in this form (same language/script as the answer, 2–3 short
  natural follow-up questions the user is likely to ask next, each ≤ 60 chars, answerable from the registry):
  FOLLOWUPS: question 1 | question 2 | question 3
- Numbers: thousands separated by spaces (1 234 567,89 for RU; 1 234 567,89 for UZ), so'm = «сум»/«so‘m», max 2 decimals.
"""

TOOLS = [
    {
        "name": "run_sql",
        "description": "Execute ONE read-only DuckDB SELECT/WITH query over v_deals, deals, contracts, participants, brokers, "
                       "usd_rates. Returns columns + up to 60 rows. Use aggregates; use LIMIT for listings.",
        "input_schema": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "DuckDB SQL"},
            "purpose": {"type": "string", "description": "Short note (in user's language) what this query checks"}},
            "required": ["sql"]},
    },
    {
        "name": "search_entities",
        "description": "Fuzzy-search companies (by name or INN), brokers (by name or code) or products (by any name in RU/UZ, "
                       "Latin/Cyrillic, typos ok). Returns INN / broker_code / canonical product_family with deal counts.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "kind": {"type": "string", "enum": ["company", "broker", "product"]}},
            "required": ["query", "kind"]},
        "cache_control": {"type": "ephemeral"},
    },
]

_client = None


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _exec_tool(name, args):
    if name == "run_sql":
        return run_sql(args.get("sql", ""))
    if name == "search_entities":
        return search_entities(args.get("query", ""), args.get("kind", "company"))
    return {"error": f"unknown tool {name}"}


def run_agent(question: str, history=None, ui_lang: str = "ru"):
    """Generator of events: {'type': 'step'|'text'|'reset'|'done'|'error', ...}."""
    messages = []
    for h in (history or [])[-6:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": str(h["content"])[:4000]})
    messages.append({"role": "user", "content": f"[ui:{ui_lang}] " + question[:2000]})
    system = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
    evidence = []
    t0 = time.time()
    for step in range(MAX_STEPS):
        with client().messages.stream(model=MODEL, max_tokens=2500, system=system, tools=TOOLS,
                                      messages=messages) as stream:
            streamed = False
            for ev in stream:
                if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                    streamed = True
                    yield {"type": "text", "text": ev.delta.text}
            msg = stream.get_final_message()
        tool_uses = [b for b in msg.content if b.type == "tool_use"]
        if not tool_uses:
            answer = fix_uz_cyrillic("".join(b.text for b in msg.content if b.type == "text"))
            followups = []
            m = re.search(r"\n*\s*FOLLOWUPS:\s*(.+?)\s*$", answer, re.S)
            if m:
                followups = [f.strip(" -•*_") for f in m.group(1).split("|") if f.strip(" -•*_")][:3]
                answer = answer[:m.start()].rstrip()
            yield {"type": "done", "evidence": evidence, "seconds": round(time.time() - t0, 1), "answer": answer,
                   "followups": followups}
            return
        if streamed:
            yield {"type": "reset"}
        messages.append({"role": "assistant", "content": msg.content})
        results = []
        for tu in tool_uses:
            yield {"type": "step", "tool": tu.name, "input": tu.input}
            res = _exec_tool(tu.name, tu.input)
            ev = {"tool": tu.name, "input": tu.input, "result": res}
            evidence.append(ev)
            yield {"type": "step_result", "tool": tu.name, "result": res}
            results.append({"type": "tool_result", "tool_use_id": tu.id,
                            "content": json.dumps(res, ensure_ascii=False, default=str)[:15000]})
        messages.append({"role": "user", "content": results})
    yield {"type": "error", "message": "Step limit reached"}


if __name__ == "__main__":
    import sys
    for e in run_agent(" ".join(sys.argv[1:])):
        if e["type"] == "text":
            print(e["text"], end="", flush=True)
        elif e["type"] == "step":
            print(f"\n[{e['tool']}] {json.dumps(e['input'], ensure_ascii=False)}")
        elif e["type"] == "step_result":
            r = e["result"]
            print(f"  -> {str(r)[:300]}")
        elif e["type"] == "done":
            print(f"\n--- {e['seconds']}s")
