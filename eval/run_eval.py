"""Accuracy eval: runs the agent on questions with SQL-verified reference answers.

Usage: python eval/run_eval.py [--only N]
A numeric check passes if the answer contains a number within 0.5 % of the reference
(also accepting thousand/million/billion/trillion scaling: "12,77 трлн сум").
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
import agent  # noqa: E402

CASES = [
    # (question, [expected numbers], [expected substrings], kind)
    ("Какой объём торгов хлопком-сырцом в 2026 году в сумах?", [12765884755818.03], [], "num"),
    ("2026-yilda paxta xomashyosi bo‘yicha nechta bitim tuzilgan (bekor qilinganlarsiz)?", [21321], [], "num"),
    ("Топ-5 покупателей карбамида по объёму за всё время", [70911], ["975117742", "Fo‘sor"], "num"),
    ("Сколько сделок было аннулировано в 2024 году и на какую сумму в сумах?", [7323, 3176768921008.54], [], "num"),
    ("Qaysi broker eng ko‘p bitimda ishtirok etgan (sotuvchi yoki xaridor tomonidan)?", [30256], ["BK111"], "num"),
    ("Экспорт медного катода в долларах: 2025 против 2026", [136296573.79, 171514722.85], [], "num"),
    ("Средневзвешенная цена бензина АИ-92 в 2025 году за литр?", [10697.40], [], "num"),
    ("Расскажи про сделку номер 400000", [230212731], ["Fuxum", "Bebek"], "num"),
    ("Сколько сделок заключено 30 октября 2026 года?", [964], [], "num"),
    ("Какой курс доллара был 15 июня 2025 года?", [12459.47], [], "num"),
    ("Qaysi hududda eng ko‘p bitim tuzilgan?", [91799], ["Toshkent"], "num"),
    # linguistics: typos, scripts, new 2026 alphabet, Russian in Latin letters
    ("скока сделок по хлопку сырцу в 2026 было без анулированых", [21321], [], "num"),
    ("2026 йилда пахта хомашёси бўйича нечта битим тузилган?", [21321], ["битим", "!bitim"], "num"),
    ("2026-yilda paxta xomaşyosi böyicha neçta bitim tuzilgan?", [21321], ["ta bitim", "!ş", "!ö", "!ç"], "num"),
    ("skolko sdelok bylo 30 oktyabrya 2026", [964], [], "num"),
    ("Какая цена пшеницы будет в декабре 2026 года?", [], [], "nodata"),
    ("Сколько сделок по золоту было в 2025 году?", [], [], "nodata"),
    ("Какой номер телефона у покупателя RANA BIZNES AJ?", [], [], "nodata"),
    ("2021-yilda qancha bitim bo‘lgan?", [], [], "nodata"),
]

NODATA = re.compile(r"нет|не содерж|отсутств|не найден|недоступ|невозможно|yo[‘ʻ']q|mavjud emas|topilmadi|"
                    r"ma’lumot.*(yo|emas)|не располага|не охватыва|не может|qamrab olmaydi|imkonsiz", re.I)


def numbers(text):
    t = re.sub(r"(?<=\d)[\s  ](?=\d{3}\b)", "", text)
    out = []
    for m in re.finditer(r"\d+(?:[.,]\d+)?", t):
        try:
            out.append(float(m.group().replace(",", ".")))
        except ValueError:
            pass
    return out


def check(answer, exp_nums, exp_strs, kind):
    if kind == "nodata":
        return bool(NODATA.search(answer)), "no-data phrase"
    nums = numbers(answer)
    miss = []
    for e in exp_nums:
        ok = any(abs(n * s - e) <= max(abs(e) * 0.005, 0.01) for n in nums for s in (1, 1e3, 1e6, 1e9, 1e12))
        if not ok:
            miss.append(e)
    miss += [s for s in exp_strs if not s.startswith("!") and s.lower() not in answer.lower()]
    miss += [s for s in exp_strs if s.startswith("!") and s[1:] in answer]
    return not miss, ("missing " + json.dumps(miss, ensure_ascii=False)) if miss else "ok"


def run(case):
    q, nums, strs, kind = case
    t = time.time()
    ans, n_sql = "", 0
    for ev in agent.run_agent(q):
        if ev["type"] == "done":
            ans = ev["answer"]
        if ev["type"] == "step" and ev["tool"] == "run_sql":
            n_sql += 1
    ok, why = check(ans, nums, strs, kind)
    return {"q": q, "ok": ok, "why": why, "sec": round(time.time() - t, 1), "sql": n_sql, "answer": ans}


if __name__ == "__main__":
    cases = CASES[: int(sys.argv[2])] if "--only" in sys.argv else CASES
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(run, cases))
    for r in res:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['sec']:>5}s  sql={r['sql']}  {r['q']}  [{r['why']}]")
        if not r["ok"]:
            print("      " + r["answer"].replace("\n", "\n      ")[:1200])
    n = sum(r["ok"] for r in res)
    print(f"\nACCURACY: {n}/{len(res)} = {100 * n / len(res):.0f}%   avg {sum(r['sec'] for r in res) / len(res):.1f}s")
    Path(__file__).with_name("last_results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
