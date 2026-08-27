from __future__ import annotations
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
from backtest import run_backtest, _compute_metrics
from round3_experiments import _window_metrics
MP=ROOT/'data/universe_manifest.json'; DP=ROOT/'data/rebalance_dates_monthly.json'
BASE={'entry_yield':7.5,'hold_yield':5.5,'max_holdings':2,'rebalance_threshold':2.0,'execution_lag_days':1,'pool_min_consecutive_years':3,'momentum_months':4,'momentum_threshold':.85,'reinvest_cash_reserve':0,'rank_by':'yield','momentum_periods':'','max_yield':999.0,'initial_capital':100000}
def run_window(start, hold, dates):
    ds=[d for d in dates if d>=start]
    r=dict(BASE, hold_yield=hold)
    q=run_backtest(rules=r,dynamic_pool=True,manifest_path=str(MP),rebalance_dates=ds,verbose=False)
    n=q.get('nav_series') or []
    return {'start':start,'end':ds[-1],'signal_count':len(ds),'hold_yield':hold,'metrics':q.get('metrics') or {},'rolling36':_window_metrics(n,36),'rolling48':_window_metrics(n,48),'oos':{y: ({'observations':len([x for x in n if str(x.get('date',''))>=y+'-01-01'])} if len([x for x in n if str(x.get('date',''))>=y+'-01-01'])<2 else _compute_metrics([x for x in n if str(x.get('date',''))>=y+'-01-01'],100000)) for y in ('2021','2023','2025')}}
def main():
 d=json.loads(DP.read_text(encoding='utf-8')); dates=d.get('dates',d); m=json.loads(MP.read_text(encoding='utf-8')); starts=['2018-01-01','2020-01-01','2021-01-01','2022-01-01','2023-01-01']
 rows=[]
 for s in starts:
  print('window',s,flush=True); a=run_window(s,5.5,dates); b=run_window(s,5.6,dates); rows.append({'start':s,'end':a['end'],'hold_5.5':a,'hold_5.6':b,'difference':{'cagr_pp':round(b['metrics'].get('cagr',0)-a['metrics'].get('cagr',0),4),'sharpe':round(b['metrics'].get('sharpe',0)-a['metrics'].get('sharpe',0),4),'max_drawdown_pp':round(b['metrics'].get('max_drawdown',0)-a['metrics'].get('max_drawdown',0),4)}})
 out={'round':12,'method':'真实重置子区间 walk-forward；每窗口初始资金10万元；显式日期；动态候选池；不使用未来函数','base_rules':BASE,'starts':starts,'manifest_records_sha256':m.get('records_sha256'),'dates_sha256':d.get('dates_sha256'),'data_cutoff':m.get('as_of'),'windows':rows,'audit':{'initial_capital':100000,'dynamic_pool':True,'execution_lag_days':1,'future_function_check':'通过：每窗口仅使用起点及之后信号，成交滞后1个交易日'}}
 (ROOT/'data/round12_walkforward_hold.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print(json.dumps([{'start':x['start'],'cagr55':x['hold_5.5']['metrics'].get('cagr'),'cagr56':x['hold_5.6']['metrics'].get('cagr'),'diff':x['difference']} for x in rows],ensure_ascii=False,indent=2))
if __name__=='__main__': main()
