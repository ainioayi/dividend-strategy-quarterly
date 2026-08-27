"""第17轮：分红信息可得性字段审计（保守滞后代理）。"""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from backtest import run_backtest
from round3_experiments import _continuous_metrics, _window_metrics
MP=ROOT/'data/universe_manifest.json';DP=ROOT/'data/rebalance_dates_monthly.json'
BASE={"entry_yield":7.5,"hold_yield":5.5,"max_holdings":2,"rebalance_threshold":2.0,"execution_lag_days":1,"pool_min_consecutive_years":3,"momentum_months":4,"momentum_threshold":.85,"rank_by":"yield","max_yield":999.0,"initial_capital":100000}
def main():
 d=json.loads(DP.read_text(encoding='utf-8')); dates=d.get('dates',d); m=json.loads(MP.read_text(encoding='utf-8'))
 rows=[]
 for lag in (0,30,60,90):
  variants=[]
  for mult in (1,3):
   rr=dict(BASE,dividend_information_lag_days=lag)
   if mult==3: rr.update(buy_commission_rate=.0009,sell_commission_rate=.0009,stamp_duty_rate=.0015,transfer_fee_rate=.00003,min_commission=15)
   x=run_backtest(rules=rr,dynamic_pool=True,manifest_path=str(MP),rebalance_dates=dates,verbose=False); nav=x.get('nav_series') or []; mm=x.get('metrics',{})
   variants.append({
    "cost_multiple": mult,
    "rules": rr,
    "metrics": mm,
    "rolling36": _window_metrics(nav, 36),
    "rolling48": _window_metrics(nav, 48),
    "oos": {y: _continuous_metrics(nav, y + '-01-01') for y in ('2021', '2023', '2025')},
   })
  rows.append({"ex_date_lag_days":lag,"variants":variants})
 out={"round":17,"method":"ex_date 可得性延迟审计；0/30/60/90天真实过滤","experiments":rows,"inputs":{"manifest_records_sha256":m.get('records_sha256'),"dates_sha256":d.get('dates_sha256'),"data_cutoff":m.get('as_of')},"audit":{"observable_fields":["ex_date","dps","bonus_ratio","transfer_ratio"],"missing_fields":["公告日","登记日"],"future_function_check":"通过：信号和动态池按 signal_date - dividend_information_lag_days 过滤 ex_date；分红入账仍使用原始 ex_date","oos_definition":"从完整账本 NAV 切片，不重新初始化现金和持仓；不是独立重置窗口","limitation":"没有公告日/登记日字段，30/60/90 天是对 ex_date 的保守代理，不代表真实信息发布延迟","survivorship_bias":"manifest 为冻结事后集合"}}
 (ROOT/'data/round17_information_lag.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print(json.dumps(rows,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
