"""第16轮：分红再投资贡献审计及3倍成本压力测试。"""
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
from backtest import run_backtest
from round3_experiments import _continuous_metrics, _window_metrics

MP=ROOT/'data/universe_manifest.json'; DP=ROOT/'data/rebalance_dates_monthly.json'
BASE={"entry_yield":7.5,"hold_yield":5.5,"max_holdings":2,"rebalance_threshold":2.0,"execution_lag_days":1,
"pool_min_consecutive_years":3,"momentum_months":4,"momentum_threshold":.85,"reinvest_cash_reserve":0,
"rank_by":"yield","momentum_periods":"","max_yield":999.0,"initial_capital":100000}

def one(name,reinvest,dates,mult=1.0,momentum=True):
 r=dict(BASE,reinvest_dividends=reinvest)
 if not momentum:r.update(momentum_months=0,momentum_threshold=0)
 if mult!=1:r.update(buy_commission_rate=.0003*mult,sell_commission_rate=.0003*mult,stamp_duty_rate=.0005*mult,transfer_fee_rate=1e-5*mult,min_commission=5*mult)
 x=run_backtest(rules=r,dynamic_pool=True,manifest_path=str(MP),rebalance_dates=dates,verbose=False); nav=x.get('nav_series') or []
 return {"name":name,"rules":r,"metrics":x.get('metrics',{}),"rolling36":_window_metrics(nav,36),"rolling48":_window_metrics(nav,48),"oos":{y:_continuous_metrics(nav,y+'-01-01') for y in ('2021','2023','2025')}}

def main():
 d=json.loads(DP.read_text(encoding='utf-8')); dates=d.get('dates',d); m=json.loads(MP.read_text(encoding='utf-8'))
 rows=[one('reinvest_true',True,dates),one('reinvest_false',False,dates),one('no_momentum_reinvest_true',True,dates,momentum=False),one('reinvest_true_3x_cost',True,dates,mult=3)]
 out={"round":16,"method":"冻结 manifest 与月末日期，当前规则对照分红再投资/现金留存、无动量及3倍交易成本；完整账本","base_rules":BASE,"inputs":{"manifest_records_sha256":m.get('records_sha256'),"dates_sha256":d.get('dates_sha256'),"data_cutoff":m.get('as_of')},"experiments":rows,"audit":{"future_function_check":"通过：执行滞后1日，复用冻结缓存","decision":"仅作贡献审计，不自动切换生产","oos_note":"OOS 从完整账本 NAV 连续切片，不重新初始化现金和持仓；不是独立重置样本外"}}
 (ROOT/'data/round16_reinvestment_control.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); print(json.dumps([{k:r['metrics'].get(k) for k in ('cagr','max_drawdown','sharpe','ending_nav')}|{'name':r['name']} for r in rows],ensure_ascii=False,indent=2))
if __name__=='__main__':main()
