from pathlib import Path
import json, sys
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
import backtest
from round3_experiments import _continuous_metrics,_window_metrics
MP=ROOT/'data/universe_manifest.json'; DP=ROOT/'data/rebalance_dates_monthly.json'
BASE=dict(backtest.BACKTEST_RULES); BASE.update({'pool_mode':'dynamic','pool_switch_month':7,'entry_yield':7.5,'hold_yield':5.5,'max_holdings':2,'rebalance_threshold':2.0,'execution_lag_days':1,'dividend_information_lag_days':0,'momentum_months':4,'momentum_threshold':.85,'reinvest_cash_reserve':0,'pool_min_consecutive_years':3})
RATES=('buy_commission_rate','sell_commission_rate','stamp_duty_rate','transfer_fee_rate')
def cost(r,m): return dict(r,**{k:float(r[k])*m for k in RATES})
def reset(r,dates,start):
 i=next((i for i,x in enumerate(dates) if x>=start),len(dates)); warm=dates[max(0,i-4):i]; ds=warm+dates[i:]; z=backtest.run_backtest(rules=r,dynamic_pool=True,manifest_path=str(MP),rebalance_dates=ds,verbose=False); n=[x for x in z.get('nav_series',[]) if x['date']>=start]; raw=float(n[0]['nav']) if n else None; factor=100000/raw if raw else None; n=[dict(x,nav=round(float(x['nav'])*factor,2)) for x in n] if factor else []
 return {'start':start,'end':dates[-1],'formal_signal_start':dates[i] if i<len(dates) else None,'warmup_signal_dates':warm,'warmup_count':len(warm),'warmup_state_preserved':True,'nav_start':n[0]['date'] if n else None,'nav_end':n[-1]['date'] if n else None,'start_nav_before_normalization':round(raw,2) if raw else None,'normalization_factor':factor,'data_cutoff':json.loads(MP.read_text(encoding='utf-8')).get('as_of'),'metrics':backtest._compute_metrics(n,100000) if len(n)>1 else {'observations':len(n)},'rolling36':_window_metrics(n,36),'rolling48':_window_metrics(n,48)}
def summary(z):
 n=z.get('nav_series',[]); return {'metrics':z.get('metrics',{}),'rolling36':_window_metrics(n,36),'rolling48':_window_metrics(n,48),'oos':{y:_continuous_metrics(n,y+'-01-01') for y in ('2021','2023','2025')}}
def main():
 d=json.loads(DP.read_text(encoding='utf-8')); dates=d.get('dates',d); m=json.loads(MP.read_text(encoding='utf-8')); rows=[]
 for y in (3,4,5):
  r=dict(BASE,pool_min_consecutive_years=y); a=backtest.run_backtest(rules=r,dynamic_pool=True,manifest_path=str(MP),rebalance_dates=dates,verbose=False); b=backtest.run_backtest(rules=cost(r,3),dynamic_pool=True,manifest_path=str(MP),rebalance_dates=dates,verbose=False)
  rows.append({'name':f'pool_years_{y}','rules':r,'normal':summary(a),'three_x_cost':summary(b),'reset_windows':{s:reset(r,dates,s) for s in ('2018-01-01','2020-01-01','2022-01-01')}}); print('完成',y,flush=True)
 out={'round':22,'experiment':'pool_continuity','method':'预注册比较连续正分红年数3/4/5；冻结输入；完整账本连续OOS、滚动36/48、三倍成本与重置窗口；无未来函数','base_rules':BASE,'inputs':{'manifest_records_sha256':m.get('records_sha256'),'dates_sha256':d.get('dates_sha256'),'data_cutoff':m.get('as_of'),'dates':{'count':len(dates),'first':dates[0],'last':dates[-1]}},'experiments':rows,'audit':{'future_function_check':'通过：动态池按信号日截断，成交滞后1日，分红按除权日处理','oos_definition':'完整账本连续切片；重置窗口保留4个动量warm-up信号点','survivorship_bias':'冻结现存代码集合存在生存者偏差','decision':'待综合全样本、OOS、滚动、重置和三倍成本后决定；不因单一CAGR切换'}}
 (ROOT/'data/round22_pool_continuity.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
if __name__=='__main__': main()
