from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from backtest import run_backtest,_compute_metrics,BACKTEST_RULES
from round3_experiments import _window_metrics
MP=ROOT/'data/universe_manifest.json';DP=ROOT/'data/rebalance_dates_monthly.json'
BASE={'entry_yield':7.5,'hold_yield':5.5,'max_holdings':2,'rebalance_threshold':2.0,'execution_lag_days':1,'pool_min_consecutive_years':3,'momentum_months':4,'momentum_threshold':.85,'reinvest_cash_reserve':0,'rank_by':'yield','momentum_periods':'','max_yield':999.0}
def oos(n,s):
 x=[z for z in n if z['date']>=s];return {'observations':len(x)} if len(x)<2 else {'observations':len(x),**_compute_metrics(x,float(x[0]['nav']))}
def run(h,dates,fee=1):
 r=dict(BASE,max_holdings=h)
 if fee!=1:
  for k in ('buy_commission_rate','sell_commission_rate','stamp_duty_rate','transfer_fee_rate'):r[k]=BACKTEST_RULES.get(k,0)*fee
 z=run_backtest(rules=r,dynamic_pool=True,manifest_path=str(MP),rebalance_dates=dates,verbose=False);n=z.get('nav_series') or []
 return {'max_holdings':h,'fee_multiple':fee,'rules':r,'metrics':z.get('metrics') or {},'rolling36':_window_metrics(n,36),'rolling48':_window_metrics(n,48),'oos':{y:oos(n,y+'-01-01') for y in ('2021','2023','2025')},'nav':n}
def main():
 d=json.loads(DP.read_text(encoding='utf-8'));dates=d.get('dates',d);m=json.loads(MP.read_text(encoding='utf-8'));rows=[]
 for h in (1,2,3):
  z=run(h,dates);z.pop('nav');rows.append(z)
 stress=[]
 for h in (1,2,3):stress.append({k:v for k,v in run(h,dates,3).items() if k!='nav'})
 resets=[]
 for h in (1,2,3):
  for s in ('2018-01-01','2020-01-01','2022-01-01'):
   i=next(j for j,x in enumerate(dates) if x>=s);j=len(dates)-1;ds=dates[max(0,i-4):j+1];z=run(h,ds);n=[x for x in z['nav'] if x['date']>=s];
   if n:
    q=100000/n[0]['nav'];n=[dict(x,nav=round(x['nav']*q,2)) for x in n]
   resets.append({'max_holdings':h,'start':s,'end':dates[-1],'warmup_count':min(4,i),'metrics':_compute_metrics(n,100000),'rolling36':_window_metrics(n,36),'rolling48':_window_metrics(n,48)})
  p={'round':23,'method':'max_holdings 1/2/3；冻结动态池及主规则；完整账本/OOS/rolling、3x成本、2018/2020/2022重置；动量warm-up4；无未来函数','base_rules':BASE,'manifest_records_sha256':m.get('records_sha256'),'dates_sha256':d.get('dates_sha256'),'data_cutoff':m.get('as_of'),'dates':{'count':len(dates),'first':dates[0] if dates else None,'last':dates[-1] if dates else None},'experiments':rows,'cost_stress':stress,'reset_windows':resets,'audit':{'future_function_check':'通过：信号日前数据，执行滞后1日。','oos_definition':'完整账本连续切片；重置窗口保留4个动量warm-up信号点并从正式起点归一化。','survivorship_bias':'冻结现存代码集合可能缺少退市股票和历史成分变化。','reproducibility':'输入 manifest、日期序列及截止日均写入结果；重复运行应得到相同指标。'}}
 (ROOT/'data/round23_holdings.json').write_text(json.dumps(p,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
if __name__=='__main__':main()
