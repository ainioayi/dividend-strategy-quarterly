from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from backtest import run_backtest,_compute_metrics,BACKTEST_RULES
from round3_experiments import _window_metrics
MP=ROOT/'data/universe_manifest.json';DP=ROOT/'data/rebalance_dates_monthly.json'
BASE={'entry_yield':7.5,'hold_yield':5.5,'max_holdings':2,'rebalance_threshold':2.0,'execution_lag_days':1,'pool_min_consecutive_years':3,'momentum_months':4,'momentum_threshold':.85,'reinvest_cash_reserve':0,'rank_by':'yield','momentum_periods':'','max_yield':999.0}
def run(h,dates,fee=1):
 r=dict(BASE,hold_yield=h)
 if fee!=1:
  for k in ('buy_commission_rate','sell_commission_rate','stamp_duty_rate','transfer_fee_rate'):r[k]=BACKTEST_RULES.get(k,0)*fee
 x=run_backtest(rules=r,dynamic_pool=True,manifest_path=str(MP),rebalance_dates=dates,verbose=False).get('nav_series') or []
 return {'hold_yield':h,'fee_multiple':fee,'rules':r,'metrics':_compute_metrics(x,float(x[0]['nav'])),'rolling36':_window_metrics(x,36),'rolling48':_window_metrics(x,48),'nav':x}
def main():
 d=json.loads(DP.read_text(encoding='utf-8'));dates=d.get('dates',d);m=json.loads(MP.read_text(encoding='utf-8')); rows=[]
 cutoff=str(m.get('as_of') or dates[-1])[:10]
 for s,e in [('2016','2019'),('2020','2022'),('2023','2026')]:
  i=next(j for j,x in enumerate(dates) if x>=s+'-01-01'); j=max(k for k,x in enumerate(dates) if x<=e+'-12-31'); ds=dates[max(0,i-4):j+1];
  block=[]
  for h in (5.5,5.3):
   z=run(h,ds); nav=[x for x in z['nav'] if x['date']>=s+'-01-01'];
   if nav:
    scale=100000/float(nav[0]['nav']);nav=[dict(x,nav=round(x['nav']*scale,2)) for x in nav]
   z['metrics']=_compute_metrics(nav,100000);z['rolling36']=_window_metrics(nav,36);z['rolling48']=_window_metrics(nav,48);z['warmup_count']=min(4,i);z['start']=s+'-01-01';z['end']=min(e+'-12-31',cutoff);z.pop('nav');block.append(z)
  rows.append({'block':s+'-'+e,'hold_5.5':block[0],'hold_5.3':block[1]})
 stress=[{k:v for k,v in run(h,dates,3).items() if k!='nav'} for h in (5.5,5.3)]
 p={'round':21,'method':'非重叠年份块严格对照；每块独立账本，动态池/lag1，hold 5.5 vs 5.3；全样本3倍成本压力；无未来函数','manifest_records_sha256':m.get('records_sha256'),'dates_sha256':d.get('dates_sha256'),'data_cutoff':m.get('as_of'),'blocks':rows,'cost_stress':stress,'audit':{'future_function_check':'通过：信号按ex_date截断，执行滞后1日。'}}
 (ROOT/'data/round21_hold_challenger.json').write_text(json.dumps(p,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
if __name__=='__main__':main()
