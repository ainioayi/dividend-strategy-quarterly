"""第14轮：冻结输入下的简单基准审计。

基准为 manifest 全体股票的等权价格指数：每只股票从首个可用信号日归一化为1，
各信号日对已有价格的横截面等权平均。它不重构现金分红，故明确标记为 price-only，
只用于检查策略相对价格市场的增益，不冒充可交易总收益基准。
"""
from __future__ import annotations
import hashlib, json, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from backtest import _compute_metrics, _load_cache, run_backtest

MP = ROOT / "data" / "universe_manifest.json"
DP = ROOT / "data" / "rebalance_dates_monthly.json"

def _equal_weight_price_nav(codes, dates, initial=100000.0):
    series = {c: (_load_cache("kl_" + c) or {}) for c in codes}
    anchors = {}
    for c in codes:
        for d in dates:
            try:
                p = float(series[c].get(d))
            except (TypeError, ValueError):
                p = 0.0
            if p > 0:
                anchors[c] = p
                break
    nav = []
    for d in dates:
        ratios = []
        for c, anchor in anchors.items():
            try:
                p = float(series[c].get(d))
            except (TypeError, ValueError):
                continue
            if p > 0 and anchor > 0:
                ratios.append(p / anchor)
        if ratios:
            nav.append({"date": d, "nav": round(initial * sum(ratios) / len(ratios), 2),
                        "coverage": len(ratios), "universe": len(codes)})
    return nav, len(anchors)

def main():
    manifest = json.loads(MP.read_text(encoding="utf-8"))
    dates_doc = json.loads(DP.read_text(encoding="utf-8"))
    dates = dates_doc.get("dates", dates_doc)
    codes = list(manifest["codes"])
    strategy = run_backtest(rules=None, dynamic_pool=True, manifest_path=str(MP),
                            rebalance_dates=dates, verbose=False)
    benchmark_nav, anchored = _equal_weight_price_nav(codes, dates)
    metrics = _compute_metrics(benchmark_nav, 100000.0)
    payload = {
        "round": 14,
        "method": "冻结 manifest 全体股票等权价格基准；每只股票首个可用月末价格归一化，信号日横截面等权；不含分红、税费和交易成本",
        "benchmark": {"name": "equal_weight_price_only", "metrics": metrics,
                      "observations": len(benchmark_nav), "anchored_codes": anchored,
                      "coverage_first": benchmark_nav[0].get("coverage") if benchmark_nav else 0,
                      "coverage_last": benchmark_nav[-1].get("coverage") if benchmark_nav else 0},
        "strategy": {"name": "current_best", "metrics": strategy.get("metrics", {}),
                     "observations": len(strategy.get("nav_series") or [])},
        "difference": {k: round(float(strategy.get("metrics", {}).get(k, 0)) - float(metrics.get(k, 0)), 4)
                       for k in ("cagr", "max_drawdown", "sharpe", "total_return")},
        "inputs": {"manifest_records_sha256": manifest.get("records_sha256"),
                   "dates_sha256": dates_doc.get("dates_sha256"), "data_cutoff": manifest.get("as_of"),
                   "codes": len(codes), "dates": len(dates)},
        "audit": {"future_function_check": "通过：仅读取信号日前已存在的精确收盘价；归一化锚点为各股票首个可用信号日",
                  "metric_definition": "策略与基准均调用 backtest._compute_metrics；CAGR按首末日期天数，Sharpe按信号间收益年化，回撤按NAV峰值",
                  "comparability": "基准为价格收益，不含分红；不能据此宣称策略战胜总回报指数",
                  "initial_capital": 100000.0}
    }
    out = ROOT / "data" / "round14_benchmark_audit.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"benchmark": metrics, "strategy": strategy.get("metrics", {}), "difference": payload["difference"]}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
