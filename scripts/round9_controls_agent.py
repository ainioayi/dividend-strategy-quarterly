from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
from backtest import run_backtest,_compute_metrics
from round3_experiments import _window_metrics
MP=ROOT/'data/universe_manifest.json'; DP=ROOT/'data/rebalance_dates_monthly.json'
BASE={'entry_yield':7.5,'hold_yield':5.5,'max_holdings':2,'rebalance_threshold':2.0,'execution_lag_days':1,'pool_min_consecutive_years':3,'momentum_months':4,'momentum_threshold':.85,'reinvest_cash_reserve':0,'rank_by':'yield','momentum_periods':'','max_yield':999.0}
def win(n,s,e=None):
 x=[z for z in n if str(z.get('date',''))>=s and (e is None or str(z.get('date',''))<=e)]
 return {'observations':len(x)} if len(x)<2 else {'observations':len(x),**_compute_metrics(x,float(x[0]['nav']))}
def one(name,o,dates):
 r=dict(BASE);r.update(o); q=run_backtest(rules=r,dynamic_pool=True,manifest_path=str(MP),rebalance_dates=dates,verbose=False);n=q.get('nav_series') or []
 return {'name':name,'rules':r,'full':q.get('metrics') or {},'rolling36':_window_metrics(n,36),'rolling48':_window_metrics(n,48),'oos':{s:win(n,s+'-01-01') for s in ('2021','2023','2025')}}
def main():
 d=json.loads(DP.read_text(encoding='utf-8')); dates=d.get('dates',d); m=json.loads(MP.read_text(encoding='utf-8'))
 v=[('rank_momentum',{'rank_by':'momentum'})]+[(f'mp_{x}',{'momentum_periods':x}) for x in ('3','3,4,5','5')]+[(f'rt_{x:g}',{'rebalance_threshold':x}) for x in (1.5,2,3)]+[(f'maxy_{x}',{'max_yield':x}) for x in (15,20,30)]+[(f'hold_{x}',{'max_holdings':x}) for x in (1,2,3)]
 rows=[]
 for i,(n,o) in enumerate(v,1): print(f'[{i}/{len(v)}] {n}',flush=True);rows.append(one(n,o,dates))
 p={'round':9,'method':'显式基线控制变量；完整账本连续切片 OOS；不重新初始化账户','base_rules':BASE,'manifest_records_sha256':m.get('records_sha256'),'dates_sha256':d.get('dates_sha256'),'data_cutoff':m.get('as_of'),'dates':{'count':len(dates),'first':dates[0],'last':dates[-1]},'experiments':rows}
 (ROOT/'data/round9_controls_agent.json').write_text(json.dumps(p,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print(json.dumps(sorted([{'name':x['name'],'cagr':x['full'].get('cagr'),'dd':x['full'].get('max_drawdown'),'sharpe':x['full'].get('sharpe'),'r36':x['rolling36'].get('min_cagr'),'oos21':x['oos']['2021'].get('cagr'),'oos23':x['oos']['2023'].get('cagr'),'oos25':x['oos']['2025'].get('cagr')} for x in rows],key=lambda x:x['cagr'] or -999,reverse=True),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
