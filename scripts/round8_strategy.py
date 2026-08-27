"""第8轮：entry_yield 邻域及持仓上限单因素实验。"""
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from backtest import _compute_metrics, run_backtest
from round3_experiments import _window_metrics
MANIFEST_PATH = ROOT / "data" / "universe_manifest.json"
DATES_PATH = ROOT / "data" / "rebalance_dates_monthly.json"
BASE = {"entry_yield": 7.5, "hold_yield": 5.5, "max_holdings": 2,
        "rebalance_threshold": 2.0, "execution_lag_days": 1,
        "pool_min_consecutive_years": 3, "momentum_months": 4,
        "momentum_threshold": 0.85, "reinvest_cash_reserve": 0}
def window(nav, start, end=None):
    s=[x for x in nav if str(x.get("date",""))>=start and (end is None or str(x.get("date",""))<=end)]
    return {"observations":len(s)} if len(s)<2 else {"observations":len(s),"start":s[0]["date"],"end":s[-1]["date"],**_compute_metrics(s,float(s[0]["nav"]))}
def run(name, overrides, dates):
    rules=dict(BASE); rules.update(overrides)
    r=run_backtest(rules=rules,dynamic_pool=True,manifest_path=str(MANIFEST_PATH),rebalance_dates=dates,verbose=False)
    n=r.get("nav_series") or []
    return {"name":name,"rules":rules,"full":r.get("metrics") or {},"rolling36":_window_metrics(n,36),"rolling48":_window_metrics(n,48),"blocks":{"2016_2018":window(n,"2016-01-01","2018-12-31"),"2019-2021":window(n,"2019-01-01","2021-12-31"),"2022-2024":window(n,"2022-01-01","2024-12-31"),"2025-2026":window(n,"2025-01-01")},"oos_continuous":{"2021":window(n,"2021-01-01"),"2023":window(n,"2023-01-01"),"2025":window(n,"2025-01-01")}}
def main():
    dp=json.loads(DATES_PATH.read_text(encoding="utf-8")); dates=dp.get("dates",dp)
    mp=json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    variants=[(f"entry_{x:g}",{"entry_yield":x}) for x in (7.25,7.5,7.75,8.0)] + [("holdings_3",{"max_holdings":3})]
    rows=[]
    for i,(name,o) in enumerate(variants,1): print(f"[{i}/{len(variants)}] {name}",flush=True); rows.append(run(name,o,dates))
    payload={"round":8,"method":"显式当前基线；单因素邻域；完整账本连续切片 OOS；不重新初始化账户","base_rules":BASE,"manifest_records_sha256":mp.get("records_sha256"),"dates_sha256":dp.get("dates_sha256"),"data_cutoff":mp.get("as_of"),"dates":{"count":len(dates),"first":dates[0],"last":dates[-1]},"experiments":rows}
    (ROOT/"data"/"round8_strategy.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    rank=sorted(rows,key=lambda x:(float(x["full"].get("cagr",-999)),float(x["rolling36"].get("min_cagr",-999)),float(x["full"].get("sharpe",-999))),reverse=True)
    print(json.dumps([{ "name":x["name"],"cagr":x["full"].get("cagr"),"max_drawdown":x["full"].get("max_drawdown"),"sharpe":x["full"].get("sharpe"),"rolling36_min":x["rolling36"].get("min_cagr"),"oos2021":x["oos_continuous"]["2021"].get("cagr"),"oos2023":x["oos_continuous"]["2023"].get("cagr"),"oos2025":x["oos_continuous"]["2025"].get("cagr")} for x in rank],ensure_ascii=False,indent=2))
if __name__=="__main__": main()
