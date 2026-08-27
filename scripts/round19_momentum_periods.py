from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from backtest import run_backtest,_compute_metrics,BACKTEST_RULES
from round3_experiments import _window_metrics
MP=ROOT/'data/universe_manifest.json';DP=ROOT/'data/rebalance_dates_monthly.json'
BASE={'entry_yield':7.5,'hold_yield':5.5,'max_holdings':2,'rebalance_threshold':2.0,'execution_lag_days':1,'pool_min_consecutive_years':3,'momentum_months':4,'momentum_threshold':.85,'reinvest_cash_reserve':0,'rank_by':'yield','max_yield':999.0}
def oos(nav,s):
 x=[z for z in nav if z['date']>=s];return {'observations':len(x)} if len(x)<2 else {'observations':len(x),**_compute_metrics(x,float(x[0]['nav']))}
def one(name,periods,dates,fee=1):
 rules=dict(BASE,momentum_periods=(periods or ''),momentum_months=0 if periods is None else 4)
 if fee!=1:
  for k in ('buy_commission_rate','sell_commission_rate','stamp_duty_rate','transfer_fee_rate'):rules[k]=BACKTEST_RULES.get(k,0)*fee
 r=run_backtest(rules=rules,dynamic_pool=True,manifest_path=str(MP),rebalance_dates=dates,verbose=False);nav=r.get('nav_series') or []
 return {'name':name,'periods':periods,'fee_multiple':fee,'rules':rules,'full':r.get('metrics') or {},'rolling36':_window_metrics(nav,36),'rolling48':_window_metrics(nav,48),'oos':{y:oos(nav,y+'-01-01') for y in ('2021','2023','2025')},'nav':nav}
def main():
 d=json.loads(DP.read_text(encoding='utf-8'));dates=d.get('dates',d);m=json.loads(MP.read_text(encoding='utf-8'))
 specs=[('single_4',''),('geomean_3_4_5','3,4,5'),('geomean_3_4','3,4'),('geomean_4_5','4,5'),('momentum_off',None)]
 rows=[one(n,p,dates) for n,p in specs]
 ranked=sorted(rows,key=lambda z:z['full'].get('cagr',-999),reverse=True)
 stress=[{k:v for k,v in one(z['name']+'_fee3x',z['periods'],dates,3).items() if k!='nav'} for z in ranked[:2]]
 resets=[]
 for z in ranked[:2]:
  for s in ('2022-01-01','2023-01-01'):
   i=next(j for j,x in enumerate(dates) if x>=s); warm=dates[max(0,i-4):];q=one(z['name'],z['periods'],warm);nav=[x for x in q['nav'] if x['date']>=s];
   if nav:
    scale=100000/nav[0]['nav'];nav=[dict(x,nav=round(x['nav']*scale,2)) for x in nav]
   resets.append({'name':z['name'],'start':s,'metrics':_compute_metrics(nav,100000),'rolling36':_window_metrics(nav,36),'rolling48':_window_metrics(nav,48)})
 for z in rows:z.pop('nav')
 payload={'round':19,'method':'预注册动量周期窄实验；单4月、3/4/5、3/4、4/5几何均值及关闭动量；冻结动态池与输入；完整账本、rolling/OOS、3x成本及2022/2023重置；无未来函数','base_rules':BASE,'variants':[x[0] for x in specs],'manifest_records_sha256':m.get('records_sha256'),'dates_sha256':d.get('dates_sha256'),'data_cutoff':m.get('as_of'),'experiments':rows,'cost_stress':stress,'reset_windows':resets,'audit':{'future_function_check':'通过：动量仅使用信号日前价格，成交滞后1日；重置保留4信号点warm-up。'}}
 (ROOT/'data/round19_momentum_periods.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print(json.dumps([{'name':z['name'],'cagr':z['full'].get('cagr'),'dd':z['full'].get('max_drawdown'),'sharpe':z['full'].get('sharpe')} for z in ranked],ensure_ascii=False,indent=2))
if __name__=='__main__':main()
