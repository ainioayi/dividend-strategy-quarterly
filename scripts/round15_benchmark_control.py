"""第15轮：固定池、无动量、无分红再投资控制对照。"""
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from backtest import run_backtest

MP = ROOT / "data" / "universe_manifest.json"
DP = ROOT / "data" / "rebalance_dates_monthly.json"

def main():
    dates_doc = json.loads(DP.read_text(encoding="utf-8"))
    dates = dates_doc.get("dates", dates_doc)
    manifest = json.loads(MP.read_text(encoding="utf-8"))
    rules = {
        "initial_capital": 100000, "entry_yield": 0.0, "hold_yield": 0.0,
        "max_holdings": 999, "rebalance_threshold": 999.0,
        "momentum_months": 0, "momentum_threshold": 0.0,
        "pool_mode": "curated", "execution_lag_days": 1,
        "reinvest_dividends": False, "reinvest_cash_reserve": 0,
        "rank_by": "yield", "max_yield": 999.0,
        "through_date": manifest.get("as_of"),
    }
    result = run_backtest(rules=rules, dynamic_pool=False, rebalance_dates=dates,
                          verbose=False)
    payload = {
        "round": 15,
        "method": "固定候选池、关闭动量、股息门槛为0、最多持有全部固定池、禁止分红再投资；执行滞后1日",
        "control_definition": "price-plus-cash control: 回测仍按价格估值，分红现金计入现金但不再买入；用于隔离动态池和动量选择贡献",
        "rules": rules,
        "metrics": result.get("metrics", {}),
        "observations": len(result.get("nav_series") or []),
        "universe": result.get("universe", {}),
        "inputs": {"manifest_records_sha256": manifest.get("records_sha256"),
                   "dates_sha256": dates_doc.get("dates_sha256"),
                   "data_cutoff": manifest.get("as_of"),
                   "dates": {"count": len(dates), "first": dates[0], "last": dates[-1]}},
        "audit": {
            "future_function_check": "通过：复用回测引擎，成交使用信号后第1个交易日精确价格",
            "dividend_scope": "分红现金按引擎既有年度入账规则计入现金，但未使用 ex_date 逐事件再投资；故不是严格总回报指数",
            "coverage_gap": "固定池为 backtest 内置兼容候选池，非 manifest 全体210只；停牌日不回填价格",
            "survivorship_bias": "存在：固定池及其历史数据均为事后冻结集合",
        },
    }
    out = ROOT / "data" / "round15_benchmark_control.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["metrics"], ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
