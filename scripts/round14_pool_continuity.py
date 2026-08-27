"""第14轮：候选池连续分红口径的少量预注册实验。

所有筛选均要求逐笔除权日 ``ex_date <= signal_date``。脚本只替换回测
模块运行时使用的筛选函数，不修改生产/研究核心逻辑，便于复现和审计。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import backtest as bt
from backtest import run_backtest, _compute_metrics
from round3_experiments import _window_metrics

MP = ROOT / "data" / "universe_manifest.json"
DP = ROOT / "data" / "rebalance_dates_monthly.json"
BASE = {"entry_yield": 7.5, "hold_yield": 5.5, "max_holdings": 2,
        "rebalance_threshold": 2.0, "execution_lag_days": 1,
        "pool_min_consecutive_years": 3, "momentum_months": 4,
        "momentum_threshold": .85, "reinvest_cash_reserve": 0,
        "rank_by": "yield", "momentum_periods": "", "max_yield": 999.0}


def _years(divs, details, as_of):
    out = set()
    for item in details if details is not None else divs:
        year = item.get("year")
        ex = str(item.get("ex_date") or item.get("ex_dividend_date") or "")[:10]
        dps = item.get("dps", item.get("cash_div_per_share", 0))
        try:
            ok = float(dps or 0) > 0
        except (TypeError, ValueError):
            ok = False
        if isinstance(year, int) and ok and len(ex) == 10 and ex <= as_of:
            out.add(year)
    return out


def make_screen(mode):
    def screen(history, as_of, min_consecutive_years=3, dividend_details_by_code=None):
        year = int(as_of[:4]); month = int(as_of[5:7])
        latest = year - 1 if month >= 7 else year - 2
        result = []
        for code, divs in history.items():
            ys = _years(divs, (dividend_details_by_code or {}).get(code), as_of)
            if mode == "baseline_latest3":
                ok = set(range(latest - 2, latest + 1)).issubset(ys)
            elif mode == "recent4_any3_latest":
                ok = latest in ys and any(set(range(end - 2, end + 1)).issubset(ys)
                                           for end in range(latest - 2, latest + 1))
            elif mode == "recent5_any3_latest":
                ok = latest in ys and any(set(range(end - 2, end + 1)).issubset(ys)
                                           for end in range(latest - 4, latest + 1))
            elif mode == "delayed_latest3":
                end = latest - 1
                ok = set(range(end - 2, end + 1)).issubset(ys)
            else:
                raise ValueError(mode)
            if ok:
                result.append(str(code).zfill(6))
        return sorted(result)
    return screen


def _oos(nav, start):
    rows = [x for x in nav if str(x.get("date", "")) >= start]
    return {"observations": len(rows)} if len(rows) < 2 else {"observations": len(rows), **_compute_metrics(rows, float(rows[0]["nav"]))}


def run_variant(name, mode, dates, fee_mult=1.0):
    old = bt.screen_dynamic_pool
    bt.screen_dynamic_pool = make_screen(mode)
    try:
        rules = dict(BASE)
        if fee_mult != 1:
            for k in ("buy_commission_rate", "sell_commission_rate", "stamp_duty_rate", "transfer_fee_rate"):
                rules[k] = bt.BACKTEST_RULES.get(k, 0.0) * fee_mult
        result = run_backtest(rules=rules, dynamic_pool=True, manifest_path=str(MP), rebalance_dates=dates, verbose=False)
    finally:
        bt.screen_dynamic_pool = old
    nav = result.get("nav_series") or []
    return {"name": name, "mode": mode, "fee_multiple": fee_mult, "rules": rules,
            "full": result.get("metrics") or {}, "rolling36": _window_metrics(nav, 36),
            "rolling48": _window_metrics(nav, 48),
            "oos": {y: _oos(nav, f"{y}-01-01") for y in ("2021", "2023", "2025")},
            "pool_provenance": result.get("pool_provenance", [])}


def main():
    dates_doc = json.loads(DP.read_text(encoding="utf-8")); dates = dates_doc.get("dates", dates_doc)
    manifest = json.loads(MP.read_text(encoding="utf-8"))
    variants = [("baseline_latest3", "baseline_latest3"), ("recent4_any3_latest", "recent4_any3_latest"),
                ("recent5_any3_latest", "recent5_any3_latest"), ("delayed_latest3", "delayed_latest3")]
    rows = [run_variant(name, mode, dates) for name, mode in variants]
    # 对全样本最佳候选池补做三倍交易成本压力，避免单点收益误导。
    best = max(rows, key=lambda x: float(x["full"].get("cagr") or -999))
    rows.append(run_variant(best["name"] + "_fee3x", best["mode"], dates, 3.0))
    payload = {"round": 14, "method": "预注册候选池连续性窄实验；逐笔 ex_date 截断；完整账本、rolling36/48、连续OOS及三倍成本压力；无未来函数",
               "base_rules": BASE, "variants": [x[0] for x in variants],
               "manifest_records_sha256": manifest.get("records_sha256"), "dates_sha256": dates_doc.get("dates_sha256"),
               "data_cutoff": manifest.get("as_of"), "dates": {"count": len(dates), "first": dates[0], "last": dates[-1]},
               "experiments": rows,
               "audit": {"future_function_check": "通过：仅使用 ex_date <= signal_date；执行滞后沿用1个交易日。",
                         "selection_note": "recent窗口规则允许窗口内较早连续三年，但要求最新确认年度有正分红；delayed为资格延迟一年。"}}
    out = ROOT / "data" / "round14_pool_continuity.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{k: r["full"].get(k) for k in ("cagr", "max_drawdown", "sharpe")} | {"name": r["name"], "r36": r["rolling36"].get("min_cagr")} for r in rows], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
