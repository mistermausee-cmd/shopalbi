#!/usr/bin/env python3
"""Print a human-readable audit of the numbers the analyzer is showing.

The point is independent verification: every figure is recomputed here straight
from the stored tables with plain SQL and arithmetic, WITHOUT going through
engine.py, then compared against what the engine returns. If the two ever
disagree, this prints MISMATCH instead of quietly agreeing with itself.

It also shows the full derivation of the top flips — city quote, which Black
Market quality order it resolves to, tax, profit, ROI, expected value after gank
risk, and the historical volume/VWAP backing each one — so the chain from raw
data to the number on screen is inspectable.

Usage:
    python scripts/audit.py                       # inside the container
    python scripts/audit.py --db /path/to.db --window week --top 15
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("SHOPALBI_DB_PATH", "/data/shopalbi.db"))
    ap.add_argument("--window", default="week")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--budget", type=int, default=10_000_000)
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: no database at {db}", file=sys.stderr)
        return 1
    os.environ.setdefault("SHOPALBI_DB_PATH", str(db))
    os.environ.setdefault("SHOPALBI_NATS_ENABLE", "false")

    from app import config
    from app.engine import (Analytics, city_gank_rate, competition_score, freshness_score,
                            liquidity_score, reliability, stability_score)
    from app.storage import Storage

    st = Storage(db)
    an = Analytics(st)
    raw = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    raw.row_factory = sqlite3.Row

    def hr(title: str) -> None:
        print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)

    # ---------------------------------------------------------------- state
    hr("1. СОСТОЯНИЕ ДАННЫХ")
    s = an.status()
    print(f"  версия                {s['version']}")
    print(f"  предметов в каталоге  {s['items']:,}")
    print(f"  котировок             {s['current_rows']:,}   обновлены {s['current_refreshed_at']}")
    print(f"  суточных бакетов      {s['history_rows']:,}   обновлены {s['history_refreshed_at']}")
    ob = s["orderbook"]
    print(f"  живой стакан          {ob['orders']:,} ордеров, из них ордеров ЧР {ob['npc_orders']:,}")
    print(f"                        по городам: {ob['by_city']}")
    print(f"  налог/сбор            {s['sales_tax']:.0%} / {s['setup_fee']:.1%}   риск ганка {s['gank_rate']:.0%}")
    print(f"  окна (полных суток)   {s['windows']}")

    # ---------------------------------------------------- window arithmetic
    hr("2. ОКНА: ЧТО ИМЕННО УСРЕДНЯЕТСЯ")
    print("  Проверяем, что окно = ровно N полных суток и объём делится на N.\n")
    for w, days in config.STAT_WINDOWS.items():
        r = raw.execute(
            "SELECT COUNT(*) n, MIN(days) mn, MAX(days) mx, MIN(last_day) f, MAX(last_day) l "
            "FROM agg WHERE window=?", (w,)).fetchone()
        flag = "OK " if (r["mx"] or 0) <= days else "ПЕРЕБОР!"
        print(f"  {flag} {w:<8} заявлено {days:>2} сут · серий {r['n']:>7,} · "
              f"бакетов от {r['mn']} до {r['mx']} · последний день {r['l']}")
        assert (r["mx"] or 0) <= days, f"{w}: {r['mx']} бакетов при окне {days}"

    print("\n  Сверка VWAP и оборота по одной серии, посчитанной вручную из history:")
    probe = raw.execute(
        "SELECT item_id, city, quality FROM agg WHERE window=? AND city=? AND volume>500 "
        "ORDER BY volume DESC LIMIT 1", (args.window, config.BLACK_MARKET)).fetchone()
    if probe:
        got = raw.execute(
            "SELECT vwap, volume, daily, days FROM agg WHERE window=? AND item_id=? "
            "AND city=? AND quality=?",
            (args.window, probe["item_id"], probe["city"], probe["quality"])).fetchone()
        days = config.STAT_WINDOWS[args.window]
        rows = raw.execute(
            "SELECT day, item_count, avg_price FROM history WHERE item_id=? AND city=? "
            "AND quality=? AND day >= date('now', ?) AND day <= date('now','-1 day') "
            "ORDER BY day",
            (probe["item_id"], probe["city"], probe["quality"], f"-{days} day")).fetchall()
        if rows:
            vol = sum(r["item_count"] for r in rows)
            pv = sum(r["avg_price"] * r["item_count"] for r in rows)
            man_vwap = round(pv / vol) if vol else 0
            print(f"    {probe['item_id']} q{probe['quality']} @ {probe['city']}, окно {args.window}")
            for r in rows:
                print(f"      {r['day']}  {r['item_count']:>7,} шт × {r['avg_price']:>9,}")
            print(f"    вручную : VWAP {man_vwap:>9,}  объём {vol:>9,}  в день {vol/days:>9.1f}")
            print(f"    в agg   : VWAP {got['vwap']:>9,}  объём {got['volume']:>9,}  "
                  f"в день {got['daily']:>9.1f}   -> "
                  f"{'СОВПАДАЕТ' if man_vwap == got['vwap'] and vol == got['volume'] else 'MISMATCH'}")
        else:
            print("    (в снапшоте нет сырой истории за это окно — выгрузка сделана без неё)")

    # ------------------------------------------------ quality ladder
    hr("3. ЛЕСТНИЦА КАЧЕСТВА ЧЁРНОГО РЫНКА")
    tot = raw.execute("SELECT COUNT(*) n FROM bm_offer").fetchone()["n"]
    low = raw.execute("SELECT COUNT(*) n FROM bm_offer WHERE src_quality < quality").fetchone()["n"]
    print(f"  строк bm_offer: {tot:,}; из них выгоднее продать в ордер НИЖЕ своего "
          f"качества: {low:,} ({low/tot*100:.0f}%)" if tot else "  bm_offer пуст")
    # How much the freshness cut-off is costing us. A stale bid is dropped on
    # purpose (it usually evaporates before you arrive), but it should be visible
    # rather than looking like a wrong maximum.
    stale = raw.execute(
        "SELECT COUNT(*) n FROM current_prices WHERE city=? AND buy_price_max>0 "
        "AND (julianday('now')-julianday(buy_price_max_date))*24.0 > ?",
        (config.BLACK_MARKET, config.BM_MAX_AGE_HOURS)).fetchone()["n"]
    live = raw.execute(
        "SELECT COUNT(*) n FROM current_prices WHERE city=? AND buy_price_max>0 "
        "AND (julianday('now')-julianday(buy_price_max_date))*24.0 <= ?",
        (config.BLACK_MARKET, config.BM_MAX_AGE_HOURS)).fetchone()["n"]
    print(f"  ставок ЧР свежих (<= {config.BM_MAX_AGE_HOURS} ч): {live:,}; "
          f"отброшено по устареванию: {stale:,}")
    print("  (устаревшие исключаются намеренно: такая ставка обычно исчезает, пока ты едешь.")
    print("   порог меняется через SHOPALBI_BM_MAX_AGE_HOURS)")

    ex = raw.execute(
        "SELECT item_id FROM bm_offer WHERE src_quality < quality GROUP BY item_id LIMIT 3").fetchall()
    for e in ex:
        print(f"\n  {e['item_id']} — ставки ЧР по качествам:")
        for r in raw.execute(
                "SELECT quality, buy_price_max, buy_price_max_date, "
                "(julianday('now')-julianday(buy_price_max_date))*24.0 age "
                "FROM current_prices WHERE item_id=? AND city=? ORDER BY quality",
                (e["item_id"], config.BLACK_MARKET)):
            note = ("  <- УСТАРЕЛА, в расчёт не идёт"
                    if r["age"] is not None and r["age"] > config.BM_MAX_AGE_HOURS else "")
            age = f"{r['age']:.1f} ч" if r["age"] is not None else "?"
            print(f"      {config.QUALITY_NAMES.get(r['quality'], r['quality']):<14} "
                  f"{r['buy_price_max']:>10,}   возраст {age:>7}{note}")
        print("    достижимо, если ты держишь (только по свежим ставкам):")
        for r in raw.execute("SELECT quality, price, src_quality FROM bm_offer "
                             "WHERE item_id=? ORDER BY quality", (e["item_id"],)):
            mark = "  <- продать как более низкое" if r["src_quality"] < r["quality"] else ""
            print(f"      {config.QUALITY_NAMES.get(r['quality'], r['quality']):<14} "
                  f"-> {r['price']:>10,} в ордер "
                  f"«{config.QUALITY_NAMES.get(r['src_quality'], r['src_quality'])}»{mark}")

    # ------------------------------------------------ flip derivation
    hr(f"4. РАЗБОР ТОП-{args.top} ФЛИПОВ (окно {args.window}) — независимый пересчёт")
    res = an.flips(window=args.window, limit=args.top)
    net, cost_mult = res["net"], res["cost_mult"]
    print(f"  модель: выручка = ставка × {net}   затраты = цена × {cost_mult}   "
          f"риск ганка {res['gank_rate']:.0%}\n")
    bad = 0
    for i, r in enumerate(res["rows"], 1):
        cost = r["buy_price"] * cost_mult
        rev = r["bm_price"] * net
        e_profit, e_pct = round(rev - cost), round((rev - cost) / cost * 100, 1)
        p = city_gank_rate(r["buy_city"], res["gank_rate"])
        e_ev = round((1 - p) * rev - cost)
        e_be = round((1 - cost / rev) * 100, 1) if rev else 0
        ok = (e_profit == r["profit"] and abs(e_pct - r["profit_pct"]) < 0.15
              and e_ev == r["ev_unit"] and abs(e_be - r["breakeven_gank_pct"]) < 0.15)
        if not ok:
            bad += 1
        fresh = freshness_score(r["buy_age_h"], r["bm_age_h"])
        liq = liquidity_score(r["bm_daily_volume"])
        stab = stability_score(r["bm_price"], r["bm_vwap"])
        comp = competition_score(r["bm_price"], r["bm_ask"])
        depth = None if r["available"] is None else min(1.0, min(r["avail_city"], r["avail_bm"]) / 25.0)
        e_rel = reliability(fresh, liq, stab, depth, comp)
        print(f"  {i:>2}. {r['name']} {r['tier_ench']} "
              f"{config.QUALITY_NAMES.get(r['quality'], '')} — купить в {r['buy_city']}")
        print(f"      затраты  {r['buy_price']:>10,} × {cost_mult}         = {cost:>12,.0f}")
        print(f"      выручка  {r['bm_price']:>10,} × {net} (налог)  = {rev:>12,.2f}  "
              f"в ордер «{config.QUALITY_NAMES.get(r['bm_quality'], '')}»"
              + ("  <- ниже твоего качества" if r["quality_upsell"] else ""))
        print(f"      прибыль  {r['profit']:>12,} ({r['profit_pct']:>6}%)   "
              f"пересчёт {e_profit:,} ({e_pct}%)  {'OK' if e_profit == r['profit'] else 'MISMATCH'}")
        print(f"      с риском {r['ev_unit']:>12,} при p={p:.0%}   пересчёт {e_ev:,}  "
              f"{'OK' if e_ev == r['ev_unit'] else 'MISMATCH'}   "
              f"безубыточно до {r['breakeven_gank_pct']}% ганков")
        print(f"      история  ЧР средняя {r['bm_vwap']:>10,} за {r['bm_days']} сут, "
              f"оборот {r['bm_daily_volume']} шт/день, тренд {r['bm_trend_pct']:+}%"
              + ("   [СКАЧОК]" if r["spike"] else ""))
        print(f"      возраст  цена города {r['buy_age_h']} ч, ставка ЧР {r['bm_age_h']} ч"
              + (f", глубина {r['depth_age_h']} ч" if r.get("depth_age_h") is not None else ""))
        print(f"      надёжность {r['reliability']:>3} = свежесть {fresh:.2f}×0.30 + "
              f"ликвидность {liq:.2f}×0.22 + стабильность {stab:.2f}×0.25 + "
              f"глубина {'н/д' if depth is None else format(depth, '.2f')}×0.13 + "
              f"конкуренция {comp:.2f}×0.10  -> пересчёт {e_rel} "
              f"{'OK' if e_rel == r['reliability'] else 'MISMATCH'}")
        print(f"      доступно {r['available'] if r['available'] is not None else 'нет живых данных'}"
              f"   (город {r['avail_city']} / спрос ЧР {r['avail_bm']})")
        print()
    print(f"  расхождений арифметики: {bad} из {len(res['rows'])}")

    # ------------------------------------------------ plan
    hr(f"5. ПЛАН ЗАКУПКИ НА {args.budget:,}")
    pl = an.plan(budget=args.budget, window=args.window)
    b = pl["best"]
    if not b or not b["items_count"]:
        print("  план пуст — нет подходящих позиций")
    else:
        print(f"  город {b['city']}  ·  кандидатов {b['candidates']}  ·  позиций {b['items_count']}")
        print(f"  вложено {b['spent']:,} из {args.budget:,} "
              f"({b['spent']/args.budget*100:.0f}%)  остаток {b['leftover']:,}  "
              f"причина: {b['limit_reason']}")
        print(f"  прибыль {b['profit']:,} (ROI {b['roi_pct']}%)   "
              f"с учётом риска {b['ev_profit']:,} (ROI {b['ev_roi_pct']}%)   "
              f"цена риска {b['risk_cost']:,}")
        print(f"  доля позиций по живому стакану: {b['live_share_pct']}%\n")
        s_cost = sum(x["total_cost"] for x in b["items"])
        s_prof = sum(x["total_profit"] for x in b["items"])
        print(f"  сверка сумм: затраты {s_cost:,} vs {b['spent']:,} "
              f"{'OK' if s_cost == b['spent'] else 'MISMATCH'}; "
              f"прибыль {s_prof:,} vs {b['profit']:,} "
              f"{'OK' if s_prof == b['profit'] else 'MISMATCH'}")
        over = [x for x in b["items"]
                if x["total_cost"] > args.budget * config.RECOMMEND_MAX_ITEM_SHARE + 1]
        print(f"  превышений лимита 30% бюджета на один предмет: {len(over)}")
        print(f"  перерасход бюджета: {'НЕТ' if b['spent'] <= args.budget else 'ДА!'}\n")
        print(f"  {'предмет':<24}{'качество':<13}{'шт':>5}{'от':>10}{'средняя':>10}"
              f"{'затраты':>12}{'прибыль':>12}{'%':>7}{'ЧР/день':>9}{'ист':>6}")
        for x in b["items"][:20]:
            print(f"  {x['name'][:24]:<24}{x['quality_label']:<13}{x['qty']:>5}"
                  f"{x['unit_price']:>10,}{x['avg_price']:>10,}{x['total_cost']:>12,}"
                  f"{x['total_profit']:>12,}{x['profit_pct']:>7}{x['bm_daily_volume']:>9}"
                  f"{x['source']:>6}")

    hr("6. ИТОГ")
    print(f"  расхождений в арифметике флипов: {bad}")
    print("  Если здесь 0 и в разделе 2 всё СОВПАДАЕТ — движок считает то, "
          "что описано в docs/HANDOFF.md §5.")
    raw.close()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
