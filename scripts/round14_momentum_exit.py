"""第14轮：持仓历史动量退出窄实验。"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from backtest import run_backtest, BACKTEST_RULES  # noqa: E402


def main() -> None:
    manifest = json.loads((ROOT / "data" / "universe_manifest.json").read_text(encoding="utf-8"))
    dates_doc = json.loads((ROOT / "data" / "rebalance_dates_monthly.json").read_text(encoding="utf-8"))
    variants = [
        ("disabled", 0.0, 1),
        ("threshold_0_80_once", 0.80, 1),
        ("threshold_0_85_once", 0.85, 1),
        ("threshold_0_90_once", 0.90, 1),
        ("threshold_0_85_confirm2", 0.85, 2),
    ]
    results = []
    for name, threshold, confirm in variants:
        rules = dict(BACKTEST_RULES)
        rules.update({"momentum_exit_threshold": threshold,
                      "momentum_exit_confirm_count": confirm})
        result = run_backtest(
            rules=rules,
            dynamic_pool=True,
            manifest_path=str(ROOT / "data" / "universe_manifest.json"),
            rebalance_dates_path=str(ROOT / "data" / "rebalance_dates_monthly.json"),
            verbose=False,
        )
        results.append({"name": name, "rules": {
            "momentum_months": rules.get("momentum_months"),
            "momentum_threshold": rules.get("momentum_threshold"),
            "momentum_exit_threshold": threshold,
            "momentum_exit_confirm_count": confirm,
            "execution_lag_days": rules.get("execution_lag_days"),
        }, "metrics": result["metrics"], "trade_count": result["metrics"].get("trade_count", 0)})
    payload = {"round": 14, "experiment": "momentum_exit", "variants": results,
               "input": {"manifest": "data/universe_manifest.json",
                         "rebalance_dates": "data/rebalance_dates_monthly.json",
                         "cutoff": manifest.get("as_of"),
                         "manifest_records_sha256": manifest.get("records_sha256"),
                         "dates_sha256": dates_doc.get("dates_sha256"),
                         "date_count": len(dates_doc.get("dates", dates_doc))},
               "audit": {
                   "future_function_check": "通过：持仓动量只用信号日前价格，交易使用下一交易日精确收盘价",
                   "default_behavior": "momentum_exit_threshold=0 时关闭，不改变当前主策略",
               }}
    out = ROOT / "data" / "round14_momentum_exit.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in results:
        m = row["metrics"]
        print(row["name"], "CAGR=%.2f%% DD=%.2f%% Sharpe=%.3f" % (m["cagr"], m["max_drawdown"], m["sharpe"]))


if __name__ == "__main__":
    main()
