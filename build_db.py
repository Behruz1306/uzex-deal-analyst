"""Build a clean, typed, normalized DuckDB from the raw UZEX CSV dump.

Usage: python build_db.py <csv_dir> <out.duckdb>
"""
import re
import sys

import duckdb

SRC, OUT = sys.argv[1], sys.argv[2]

ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}
WORDNUM = {"перв": "1", "втор": "2", "трет": "3"}

# Ordered (regex on lowercased name, family RU, family UZ). First match wins.
FAMILIES = [
    (r"хлопок.?сырец", "Хлопок-сырец", "Paxta xomashyosi"),
    (r"шрот", "Шрот хлопковый", "Paxta shroti"),
    (r"масло.*хлопк|хлопк.*масло", "Масло хлопковое", "Paxta yogʻi"),
    (r"масло.*подсолн|подсолн.*масло", "Масло подсолнечное", "Kungaboqar yogʻi"),
    (r"пряжа", "Пряжа хлопковая", "Paxta ipi"),
    (r"мука", "Мука пшеничная", "Bugʻdoy uni"),
    (r"пшениц", "Пшеница", "Bugʻdoy"),
    (r"\bрис\b", "Рис", "Guruch"),
    (r"сахар", "Сахар", "Shakar"),
    (r"\bсоль\b", "Соль пищевая", "Osh tuzi"),
    (r"вода", "Вода питьевая", "Ichimlik suvi"),
    (r"комбикорм|кормосмесь", "Комбикорм / кормосмесь", "Omuxta yem"),
    (r"цемент", "Цемент", "Sement"),
    (r"кирпич", "Кирпич", "Gʻisht"),
    (r"гипс", "Гипс", "Gips"),
    (r"известь", "Известь", "Ohak"),
    (r"стекло", "Стекло", "Shisha"),
    (r"пиломатериал", "Пиломатериалы", "Yogʻoch-taxta"),
    (r"проволок", "Проволока арматурная", "Armatura simi"),
    (r"арматур", "Арматура", "Armatura"),
    (r"лист.*сталь|сталь.*лист", "Лист стальной", "Poʻlat list"),
    (r"катод|медн", "Катод медный", "Mis katodi"),
    (r"профиль", "Профиль алюминиевый", "Alyuminiy profil"),
    (r"подшипник", "Подшипник", "Podshipnik"),
    (r"кабель", "Кабель силовой", "Kuch kabeli"),
    (r"бензин", "Бензин", "Benzin"),
    (r"дизел", "Дизельное топливо", "Dizel yoqilgʻisi"),
    (r"мазут", "Мазут", "Mazut"),
    (r"битум", "Битум", "Bitum"),
    (r"газ сжиж|сжиж.*газ|пропан", "Газ сжиженный", "Suyultirilgan gaz"),
    (r"карбамид|мочевин", "Карбамид", "Karbamid"),
    (r"селитр", "Аммиачная селитра", "Ammiakli selitra"),
    (r"полипропилен", "Полипропилен", "Polipropilen"),
    (r"труб", "Трубы полиэтиленовые", "Polietilen quvurlar"),
    (r"полиэтилен", "Полиэтилен", "Polietilen"),
    (r"поливинилхлорид|пвх", "Поливинилхлорид (ПВХ)", "Polivinilxlorid (PVX)"),
    (r"эмаль", "Эмаль", "Emal"),
    (r"аргон", "Аргон", "Argon"),
    (r"азот", "Азот", "Azot"),
    (r"кислот", "Кислота серная", "Sulfat kislota"),
    (r"сода", "Сода каустическая", "Kaustik soda"),
]


def family(name: str):
    s = (name or "").lower().replace("ё", "е")
    for rx, ru, uz in FAMILIES:
        if re.search(rx, s):
            return ru, uz
    return name, name


def norm_sort(text: str):
    s = (text or "").lower().replace("ё", "е")
    if re.search(r"высш|в/с", s):
        return "высший"
    m = re.search(r"\b(i{1,3}|iv|[1-4])\s*-?\s*сорт", s)
    if m:
        return ROMAN.get(m.group(1), m.group(1))
    m = re.search(r"(перв|втор|трет)\w*\s+сорт", s)
    if m:
        return WORDNUM[m.group(1)]
    return None


def norm_class(text: str):
    s = (text or "").lower()
    m = re.search(r"\b(i{1,3}|iv|[1-4])\s*-?\s*класс", s)
    if m:
        return ROMAN.get(m.group(1), m.group(1))
    return None


def norm_code(text):
    if not text:
        return None
    return re.sub(r"\s+", "", text).upper().replace("Х", "Х")


def base_name(name: str):
    """Product name without embedded sort/class, unified case/punctuation."""
    s = (name or "").replace("ё", "е")
    s = re.sub(r"(?i)\b(i{1,3}|iv|[1-4]|перв\w*|втор\w*|трет\w*|высш\w*)\s*-?\s*(сорт\w*|класс)", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:1].upper() + s[1:].lower() if s else s


con = duckdb.connect(OUT)
con.execute("SET threads TO 4")
csv = lambda f: f"read_csv('{SRC}/{f}', all_varchar=true, header=true)"

# ---------- contracts (normalized) ----------
raw = con.execute(f"select * from {csv('contracts.csv')}").fetchall()
cols = [d[0] for d in con.description]
ix = {c: i for i, c in enumerate(cols)}
rows = []
for r in raw:
    g = lambda c: r[ix[c]]
    name = g("Наименование продукта")
    full = " ".join(x for x in [name, g("Сорт товара"), g("Класс товара")] if x)
    fam_ru, fam_uz = family(name)
    cls_raw = g("Класс товара")
    rows.append(
        (
            int(g("Номер контракта")), name, base_name(name), fam_ru, fam_uz,
            g("Марка товара"), norm_code(g("Марка товара")) or (norm_code(cls_raw) if cls_raw and not norm_class(cls_raw) else None),
            g("Размер товара"), (re.sub(r"\s+", "", g("Размер товара")).lower() if g("Размер товара") else None),
            g("Сорт товара"), norm_sort(full), cls_raw, norm_class(full),
            g("Тип товара"), (g("Тип товара") or "").lower() or None,
            g("Код ТН ВЭД"), g("Группа товара"), g("Категория товара"), g("Отрасль промышленности"),
            g("Единица измерения"), float(g("Лот")), g("Валюта"),
            "USD" if g("Валюта") == "Доллар США" else "UZS",
            float(g("Базисная цена")), float(g("Сумма залога").replace("%", "").strip()),
            g("Базис поставки"), g("Упаковка"), g("Регион"), g("Адрес склада"),
            g("Тип торгов"), g("Тип контракта"), g("Форма контракта"),
            int(g("Срок оплаты")), int(g("Срок поставки")), g("ИНН продавца"),
            g("Дата регистрации"), g("Действует до"),
        )
    )
con.execute("""
create or replace table contracts (
  contract_id BIGINT PRIMARY KEY, product_name_raw VARCHAR, product_base VARCHAR,
  product_family VARCHAR, product_family_uz VARCHAR,
  brand_raw VARCHAR, brand VARCHAR, size_raw VARCHAR, size VARCHAR,
  grade_raw VARCHAR, grade VARCHAR, class_raw VARCHAR, class VARCHAR,
  type_raw VARCHAR, type VARCHAR,
  hs_code VARCHAR, product_group VARCHAR, category VARCHAR, industry VARCHAR,
  unit VARCHAR, lot DOUBLE, currency_raw VARCHAR, currency VARCHAR,
  base_price DOUBLE, deposit_pct DOUBLE, delivery_basis VARCHAR, packaging VARCHAR,
  warehouse_region_ru VARCHAR, warehouse_address VARCHAR,
  auction_type VARCHAR, contract_type VARCHAR, contract_form VARCHAR,
  payment_term_days INT, delivery_term_days INT, seller_inn VARCHAR,
  registered_raw VARCHAR, valid_until_raw VARCHAR)""")


# brands/sizes embedded in the name when the attribute fields are empty
known_brands = sorted({r[6] for r in rows if r[6]}, key=len, reverse=True)
known_sizes = sorted({r[8] for r in rows if r[8]}, key=len, reverse=True)
for i, r in enumerate(rows):
    r = list(r)
    nm = re.sub(r"\s+", "", r[1]).upper()
    if not r[6]:
        r[6] = next((b for b in known_brands if len(b) > 1 and b in nm), None)
    if not r[8]:
        r[8] = next((z for z in known_sizes if z.upper() in nm), None)
    rows[i] = tuple(r)

con.executemany("insert into contracts values (" + ",".join(["?"] * 37) + ")", rows)
con.execute("""
alter table contracts add column registered_on DATE;
alter table contracts add column valid_until DATE;
update contracts set registered_on = strptime(registered_raw,'%d.%m.%Y')::DATE,
                     valid_until  = strptime(valid_until_raw,'%d.%m.%Y')::DATE;
alter table contracts drop column registered_raw;
alter table contracts drop column valid_until_raw;
""")
# ---------- reference tables ----------
con.execute(f"""
create or replace table usd_rates as
select strptime("Дата",'%d.%m.%Y')::DATE as rate_date, "Курс"::DOUBLE as uzs_per_usd from {csv('usd_rates.csv')};

create or replace table participants as
select "ИНН" as inn, "Наименование" as name, "Регион" as region, "Вид деятельности" as activity,
       "Роль" as role, ("Плательщик НДС" = 'Да') as vat_payer
from {csv('participants.csv')};

create or replace table brokers as
select "Код брокера" as broker_code, "Наименование" as name, "ИНН" as inn, "Регион" as region
from {csv('brokers.csv')};
""")

# ---------- deals ----------
con.execute(f"""
create or replace table deals as
with d as (select * from {csv('deals_*.csv')})
select
  "Номер сделки"::BIGINT as deal_id,
  strptime("Дата сделки",'%d.%m.%Y')::DATE as deal_date,
  "Время сделки"::TIME as deal_time,
  "Тип торгов" as auction_type,
  "Количество"::BIGINT as qty_lots,
  replace("Сумма сделки",' ','')::DOUBLE as amount,
  replace("Стартовая цена",' ','')::DOUBLE as start_price,
  "Номер контракта"::BIGINT as contract_id,
  "Наименование товара" as item_name,
  "Лот"::DOUBLE as lot,
  "Единица измерения" as unit,
  "Регион" as region,
  "Тип контракта" as contract_type,
  "Форма контракта" as contract_form,
  "Состояние сделки" as status,
  "ИНН продавца" as seller_inn,
  "ИНН покупателя" as buyer_inn,
  "Брокер продавца" as seller_broker,
  "Брокер покупателя" as buyer_broker,
  strptime(nullif("Дата оплаты",''),'%d.%m.%Y')::DATE as paid_on,
  strptime(nullif("Дата поставки",''),'%d.%m.%Y')::DATE as delivered_on,
  nullif("Поставлено",'')::BIGINT as delivered_lots
from d;

alter table deals add column currency VARCHAR;
alter table deals add column usd_rate DOUBLE;
update deals set currency = c.currency from contracts c where c.contract_id = deals.contract_id;
update deals set usd_rate = r.uzs_per_usd from usd_rates r where r.rate_date = deals.deal_date;
""")

con.execute("""
create or replace view v_deals as
select d.*,
  year(d.deal_date) as year, month(d.deal_date) as month, quarter(d.deal_date) as quarter,
  d.qty_lots * d.lot as volume,
  d.amount / nullif(d.qty_lots * d.lot, 0) as unit_price,
  case when d.currency = 'USD' then d.amount * d.usd_rate else d.amount end as amount_uzs,
  case when d.currency = 'USD' then d.amount else d.amount / d.usd_rate end as amount_usd,
  d.status <> 'Аннулирован(Не оплачен)' as is_valid,
  d.status in ('Исполнен','Исполнен(Частично)') as is_executed,
  c.product_family, c.product_family_uz, c.product_base, c.product_name_raw,
  c.brand, c.size, c.grade, c.class, c.type, c.category, c.industry, c.product_group, c.hs_code,
  c.delivery_basis, c.packaging, c.base_price, c.payment_term_days, c.delivery_term_days,
  s.name as seller_name, b.name as buyer_name,
  sb.name as seller_broker_name, bb.name as buyer_broker_name
from deals d
join contracts c using (contract_id)
left join participants s on s.inn = d.seller_inn
left join participants b on b.inn = d.buyer_inn
left join brokers sb on sb.broker_code = d.seller_broker
left join brokers bb on bb.broker_code = d.buyer_broker;
""")

# sanity
print(con.execute("select count(*), count(currency), count(usd_rate) from deals").fetchall())
print(con.execute("select product_family, count(*) from contracts group by 1 order by 2 desc").fetchall())
print(con.execute("select grade, count(*) from contracts group by 1 order by 2 desc").fetchall())
print(con.execute("select class, count(*) from contracts group by 1 order by 2 desc").fetchall())
con.close()
