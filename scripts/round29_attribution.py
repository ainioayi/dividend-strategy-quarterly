# -*- coding: utf-8 -*-
# 第 29 轮：收益归因分析。
# 将年化收益拆分为个股、分红与资本利得、年度区间和集中度；
# 本脚本只读分析，不改变策略规则。
from __future__ import annotations
import json
import sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from backtest import run_backtest

MANIFEST = ROOT / "data" / "universe_manifest.json"
REBALANCE_DATES = ROOT / "data" / "rebalance_dates_monthly.json"
OUTPUT = ROOT / "data" / "round29_attribution.json"

# 事件方向来自 quarterly_strategy.py。保留 Unicode 转义，避免外部工具处理
# 文件时受编码设置影响。
BUY_SIDE = "\u4e70\u5165"   # 买入
SELL_SIDE = "\u5356\u51fa"  # 卖出


def main():
    result = run_backtest(
        dynamic_pool=True,
        manifest_path=str(MANIFEST),
        rebalance_dates_path=str(REBALANCE_DATES),
        rules={
            "entry_yield": 7.5,
            "hold_yield": 5.5,
            "momentum_months": 4,
            "momentum_threshold": 0.85,
            "pool_min_consecutive_years": 3,
            "pool_switch_month": 7,
            "max_holdings": 2,
            "rebalance_threshold": 2.0,
            "execution_lag_days": 1,
            "dividend_information_lag_days": 0,
            "reinvest_cash_reserve": 0,
        },
        reinvest=False,
        verbose=False,
        return_events=True,
    )

    events = result.pop("_events", [])
    nav_series = result["nav_series"]
    final_holdings = result.get("final_holdings", [])
    metrics = result["metrics"]

    buys = [e for e in events if e.get("side") in (BUY_SIDE, "buy")]
    sells = [e for e in events if e.get("side") in (SELL_SIDE, "sell")]
    dividends = [e for e in events if e.get("side") == "dividend"]
    splits = [e for e in events if e.get("side") == "split"]

    stock_buy_cost = defaultdict(float)
    stock_sell_proceeds = defaultdict(float)
    stock_div_income = defaultdict(float)
    stock_buy_shares = defaultdict(int)
    stock_sell_shares = defaultdict(int)
    stock_names = {}

    for e in buys:
        code = str(e.get("code"))
        stock_buy_cost[code] += float(e.get("net_cash", 0))
        stock_buy_shares[code] += int(e.get("shares", 0))
        if e.get("name"):
            stock_names[code] = e["name"]

    for e in sells:
        code = str(e.get("code"))
        stock_sell_proceeds[code] += float(e.get("net_cash", 0))
        stock_sell_shares[code] += int(e.get("shares", 0))
        if e.get("name"):
            stock_names[code] = e["name"]

    for e in dividends:
        code = str(e.get("code"))
        stock_div_income[code] += float(e.get("net_cash", 0))

    # 期末持仓估值。
    final_value_by_code = {}
    for h in final_holdings:
        code = str(h["code"])
        shares = int(h.get("shares", 0))
        entry_price = float(h.get("entry_price", 0))
        final_value_by_code[code] = shares * entry_price

    # 若净值快照包含实际价格，优先使用该价格。
    last_holdings_snap = nav_series[-1].get("holdings") if nav_series else None
    if last_holdings_snap:
        for code, info in last_holdings_snap.items():
            shares = int(info.get("shares", 0))
            price = float(info.get("price", info.get("entry_price", 0)))
            final_value_by_code[str(code)] = shares * price

    all_codes = sorted(
        set(stock_buy_cost.keys())
        | set(stock_sell_proceeds.keys())
        | set(stock_div_income.keys())
    )
    stock_pl = []
    for code in all_codes:
        buy_cost = stock_buy_cost[code]
        sell_proceeds = stock_sell_proceeds[code]
        div_income = stock_div_income[code]
        unrealized = final_value_by_code.get(code, 0.0)
        total_pl = sell_proceeds + unrealized + div_income - buy_cost
        cap_pl = sell_proceeds + unrealized - buy_cost
        stock_pl.append({
            "code": code,
            "name": stock_names.get(code, ""),
            "buy_cost": round(buy_cost, 2),
            "sell_proceeds": round(sell_proceeds, 2),
            "dividend_income": round(div_income, 2),
            "unrealized_value": round(unrealized, 2),
            "capital_pl": round(cap_pl, 2),
            "total_pl": round(total_pl, 2),
            "buy_shares": stock_buy_shares[code],
            "sell_shares": stock_sell_shares[code],
        })

    stock_pl.sort(key=lambda x: x["total_pl"], reverse=True)

    total_buy = sum(s["buy_cost"] for s in stock_pl)
    total_sell = sum(s["sell_proceeds"] for s in stock_pl)
    total_div = sum(s["dividend_income"] for s in stock_pl)
    total_unrealized = sum(s["unrealized_value"] for s in stock_pl)
    total_pl = sum(s["total_pl"] for s in stock_pl)
    total_cap_pl = sum(s["capital_pl"] for s in stock_pl)
    initial_capital = 100000.0
    ending_nav = float(metrics.get("ending_nav", 0))

    div_pct = (total_div / total_pl * 100) if total_pl > 0 else 0
    cap_pct = (total_cap_pl / total_pl * 100) if total_pl > 0 else 0

    sorted_pl_vals = [s["total_pl"] for s in stock_pl]
    top_n = {}
    for n in [1, 3, 5, 10]:
        top_sum = sum(sorted_pl_vals[:n])
        top_n["top_%d" % n] = {
            "pct_of_total": round(top_sum / total_pl * 100, 1) if total_pl > 0 else 0,
            "abs_pl": round(top_sum, 2),
        }

    yearly = {}
    for item in nav_series:
        year = item["date"][:4]
        if year not in yearly:
            yearly[year] = {"start_nav": item["nav"], "end_nav": item["nav"]}
        yearly[year]["end_nav"] = item["nav"]

    yearly_returns = []
    prev_end = None
    for year in sorted(yearly.keys()):
        start = yearly[year]["start_nav"] if prev_end is None else prev_end
        end = yearly[year]["end_nav"]
        ret = (end / start - 1) * 100 if start > 0 else 0
        yearly_returns.append({
            "year": year,
            "start_nav": round(start, 2),
            "end_nav": round(end, 2),
            "return_pct": round(ret, 2),
        })
        prev_end = end

    div_by_year = defaultdict(float)
    for e in dividends:
        year = str(e.get("date", ""))[:4]
        if year:
            div_by_year[year] += float(e.get("net_cash", 0))

    winning = sum(1 for s in stock_pl if s["total_pl"] > 0)
    losing = sum(1 for s in stock_pl if s["total_pl"] <= 0)
    win_rate = winning / len(stock_pl) * 100 if stock_pl else 0

    # 按代码和时间顺序匹配买卖事件，计算平均持有期。
    holding_periods = []
    buy_dates_by_code = defaultdict(list)
    for e in buys:
        buy_dates_by_code[str(e.get("code"))].append(str(e.get("date", ""))[:10])
    for e in sells:
        code = str(e.get("code"))
        sell_date = str(e.get("date", ""))[:10]
        if buy_dates_by_code[code]:
            buy_date = buy_dates_by_code[code].pop(0)
            try:
                bd = datetime.strptime(buy_date, "%Y-%m-%d")
                sd = datetime.strptime(sell_date, "%Y-%m-%d")
                holding_periods.append((sd - bd).days)
            except ValueError:
                pass

    avg_holding_days = sum(holding_periods) / len(holding_periods) if holding_periods else 0

    output = {
        "round": 29,
        "description": "收益归因：个股、分红与资本利得、年度区间和集中度",
        "data_cutoff": result.get("data_cutoff"),
        "manifest_records_sha256": (result.get("universe") or {}).get("records_sha256"),
        "baseline_metrics": {
            "cagr": metrics.get("cagr"),
            "max_drawdown": metrics.get("max_drawdown"),
            "sharpe": metrics.get("sharpe"),
            "ending_nav": metrics.get("ending_nav"),
            "trade_count": metrics.get("trade_count"),
            "dividend_event_count": metrics.get("dividend_event_count"),
        },
        "decomposition": {
            "initial_capital": initial_capital,
            "ending_nav": ending_nav,
            "total_profit": round(ending_nav - initial_capital, 2),
            "total_buy_cost": round(total_buy, 2),
            "total_sell_proceeds": round(total_sell, 2),
            "total_dividend_income_after_tax": round(total_div, 2),
            "total_unrealized_value": round(total_unrealized, 2),
            "total_capital_pl": round(total_cap_pl, 2),
            "total_pl_incl_dividends": round(total_pl, 2),
            "dividend_income_pct_of_total_pl": round(div_pct, 1),
            "capital_pl_pct_of_total_pl": round(cap_pct, 1),
        },
        "concentration": top_n,
        "win_rate": {
            "winning_positions": winning,
            "losing_positions": losing,
            "total_positions": len(stock_pl),
            "win_rate_pct": round(win_rate, 1),
            "avg_holding_days": round(avg_holding_days, 0),
        },
        "yearly_returns": yearly_returns,
        "dividend_income_by_year": {y: round(v, 2) for y, v in sorted(div_by_year.items())},
        "per_stock_pl": stock_pl,
        "event_counts": {
            "buys": len(buys),
            "sells": len(sells),
            "dividends": len(dividends),
            "splits": len(splits),
        },
        "limitations": [
            "期末持仓按入场价或最后一条净值快照价格估值",
            "买入成本包含费用，卖出收入已扣费用，分红为税后金额",
            "未逐批采用先进先出匹配，而是按股票代码汇总",
            "分红再投资买入计入普通买入事件",
            "结果依赖冻结缓存和当前策略参数",
            "不构成未来收益保证或投资建议",
        ],
    }

    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写入：", OUTPUT)

    print("\n" + "=" * 60)
    print("第 29 轮：收益归因")
    print("=" * 60)
    print("\n收益分解：")
    print("  初始资金：%.0f" % initial_capital)
    print("  期末净值：%.0f" % ending_nav)
    print("  总盈利：  %.0f" % (ending_nav - initial_capital))
    print("  分红收入：%.0f（占 %.1f%%）" % (total_div, div_pct))
    print("  资本利得：%.0f（占 %.1f%%）" % (total_cap_pl, cap_pct))

    print("\n集中度：")
    for k, v in top_n.items():
        print("  %s: %.1f%% (%.0f)" % (k, v["pct_of_total"], v["abs_pl"]))

    print("\n胜率：%d/%d = %.1f%%，平均持有 %.0f 天" % (
        winning, len(stock_pl), win_rate, avg_holding_days))

    print("\n年度收益：")
    for y in yearly_returns:
        print("  %s: %+.2f%%" % (y["year"], y["return_pct"]))

    print("\n总盈亏前 10 只股票：")
    for s in stock_pl[:10]:
        nm = s["name"][:8] if s["name"] else ""
        print("  %s %s：总计 %+.0f，资本 %+.0f，分红 %+.0f" % (
            s["code"], nm, s["total_pl"], s["capital_pl"], s["dividend_income"],
        ))

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
