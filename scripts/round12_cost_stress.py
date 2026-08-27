from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT / 'scripts'))
from backtest import run_backtest, _compute_metrics
from round3_experiments import _window_metrics
MP = ROOT / 'data/universe_manifest.json'; DP = ROOT / 'data/rebalance_dates_monthly.json'
BASE = {'entry_yield':7.5,'hold_yield':5.5,'max_holdings':2,'rebalance_threshold':2.0,'execution_lag_days':1,'pool_min_consecutive_years':3,'momentum_months':4,'momentum_threshold':.85,'reinvest_cash_reserve':0,'rank_by':'yield','momentum_periods':'','max_yield':999.0}
COST = {'buy_commission_rate':0.0003,'sell_commission_rate':0.0003,'stamp_duty_rate':0.0005,'transfer_fee_rate':0.00001}
def seg(nav, start):
    x=[z for z in nav if str(z.get('date',''))>=start]
    return {'observations':len(x)} if len(x)<2 else {'observations':len(x),**_compute_metrics(x,float(x[0]['nav']))}
def one(hold, mult, dates):
    rules=dict(BASE, hold_yield=hold); rules.update({k:v*mult for k,v in COST.items()})
    r=run_backtest(rules=rules,dynamic_pool=True,manifest_path=str(MP),rebalance_dates=dates,verbose=False); n=r.get('nav_series') or []
    return {'hold_yield':hold,'cost_multiplier':mult,'rules':rules,'full':r.get('metrics') or {},'rolling36':_window_metrics(n,36),'rolling48':_window_metrics(n,48),'oos':{s:seg(n,s+'-01-01') for s in ('2021','2023','2025')}}
def main():
    dd=json.loads(DP.read_text(encoding='utf-8')); dates=dd.get('dates',dd); m=json.loads(MP.read_text(encoding='utf-8')); rows=[]
    for h in (5.5,5.6):
        for mult in (1,2,3): print(f'hold={h} cost={mult}x',flush=True); rows.append(one(h,mult,dates))
    out={'round':12,'method':'冻结 manifest/cache；基线与 hold_yield=5.6 各做 1x/2x/3x 交易成本压力；完整账本、滚动窗口、连续 OOS；无未来数据','base_rules':BASE,'base_cost':COST,'manifest_records_sha256':m.get('records_sha256'),'dates_sha256':dd.get('dates_sha256'),'data_cutoff':m.get('as_of'),'dates':{'count':len(dates),'first':dates[0],'last':dates[-1]},'experiments':rows,'interpretation':'比较同一成本倍率下 5.6 与 5.5；成本倍率仅改变佣金、印花税、过户费，不改变信号规则。'}
    (ROOT/'data/round12_cost_stress.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps([{'hold':r['hold_yield'],'cost':r['cost_multiplier'],'cagr':r['full'].get('cagr'),'dd':r['full'].get('max_drawdown'),'sharpe':r['full'].get('sharpe'),'r36':r['rolling36'].get('min_cagr'),'r48':r['rolling48'].get('min_cagr'),'oos23':r['oos']['2023'].get('cagr')} for r in rows],ensure_ascii=False,indent=2))
if __name__=='__main__': main()
