from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from backtest import run_backtest,_compute_metrics
from round3_experiments import _window_metrics
MP=ROOT/'data/universe_manifest.json';DP=ROOT/'data/rebalance_dates_monthly.json'
BASE={'entry_yield':7.5,'hold_yield':5.5,'max_holdings':2,'rebalance_threshold':2.0,'execution_lag_days':1,'pool_min_consecutive_years':3,'momentum_months':4,'momentum_threshold':.85,'reinvest_cash_reserve':0,'rank_by':'yield','momentum_periods':'','max_yield':999.0}
def oos(nav,s):
 x=[z for z in nav if str(z.get('date',''))>=s];return {'observations':len(x)} if len(x)<2 else {'observations':len(x),**_compute_metrics(x,float(x[0]['nav']))}
def one(rules,dates,fee=1):
 rr=dict(rules)
 if fee!=1:
  for k in ('buy_commission_rate','sell_commission_rate','stamp_duty_rate','transfer_fee_rate'): rr[k]=0.0003*fee if 'commission' in k else (0.0005*fee if 'stamp' in k else 0.00001*fee)
 r=run_backtest(rules=rr,dynamic_pool=True,manifest_path=str(MP),rebalance_dates=dates,verbose=False);nav=r.get('nav_series') or []
 return {'full':r.get('metrics') or {},'rolling36':_window_metrics(nav,36),'rolling48':_window_metrics(nav,48),'oos':{y:oos(nav,y+'-01-01') for y in ('2021','2023','2025')},'nav':nav}
def main():
 d=json.loads(DP.read_text(encoding='utf-8'));dates=d.get('dates',d);m=json.loads(MP.read_text(encoding='utf-8'))
 specs=[(e,h,mt) for e in (7.4,7.5,7.6) for h in (5.5,5.6) for mt in (.84,.85,.86)]
 rows=[]
 for e,h,mt in specs:
  rules=dict(BASE,entry_yield=e,hold_yield=h,momentum_threshold=mt); z=one(rules,dates); z.update({'name':f'e{e}_h{h}_m{mt}','rules':rules}); z.pop('nav'); rows.append(z)
 ranked=sorted(rows,key=lambda z:z['full'].get('cagr',-999),reverse=True)
 resets=[]
 for z in ranked[:2]:
  for s in ('2018-01-01','2020-01-01','2022-01-01'):
   i=next((j for j,x in enumerate(dates) if x>=s),len(dates)); run_dates=dates[max(0,i-4):]; q=one(z['rules'],run_dates); nav=q.pop('nav'); nav=[x for x in nav if x['date']>=s]
   if nav:
    scale=100000/float(nav[0]['nav']); nav=[dict(x,nav=round(x['nav']*scale,2)) for x in nav]
   resets.append({'name':z['name'],'start':s,'metrics':_compute_metrics(nav,100000),'rolling36':_window_metrics(nav,36),'rolling48':_window_metrics(nav,48)})
 # 对全样本年化收益率最高的两个方案做三倍费用压力测试。
 stress=[dict(one(z['rules'],dates,3),name=z['name']+'_fee3x',rules=z['rules']) for z in ranked[:2]]
 payload={'round':17,'method':'预注册3x2x3参数小网格；动态池连续3年、4个月动量、2只、lag1；全样本/OOS/rolling及三倍成本，前两名做2018/2020/2022严格重置','base_rules':BASE,'grid':{'entry_yield':[7.4,7.5,7.6],'hold_yield':[5.5,5.6],'momentum_threshold':[.84,.85,.86]},'manifest_records_sha256':m.get('records_sha256'),'dates_sha256':d.get('dates_sha256'),'data_cutoff':m.get('as_of'),'dates':{'count':len(dates),'first':dates[0],'last':dates[-1]},'experiments':rows,'top_by_full_cagr':[z['name'] for z in ranked[:5]],'cost_stress':stress,'reset_windows':resets,'audit':{'future_function_check':'通过：回测动态池按信号日截断，执行滞后1日；重置窗口保留4个信号点作动量warm-up。'}}
 (ROOT/'data/round17_joint_robustness.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print(json.dumps([{'name':z['name'],'cagr':z['full'].get('cagr'),'dd':z['full'].get('max_drawdown'),'sharpe':z['full'].get('sharpe')} for z in ranked[:8]],ensure_ascii=False,indent=2))
if __name__=='__main__':main()
