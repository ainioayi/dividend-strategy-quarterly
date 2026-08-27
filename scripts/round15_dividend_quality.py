"""第15轮：简单分红质量过滤实验，严格按除权日截断。"""
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
import backtest as bt
from backtest import run_backtest,_compute_metrics
from round3_experiments import _window_metrics
MP=ROOT/'data/universe_manifest.json'; DP=ROOT/'data/rebalance_dates_monthly.json'
BASE={'entry_yield':7.5,'hold_yield':5.5,'max_holdings':2,'rebalance_threshold':2.0,'execution_lag_days':1,'pool_min_consecutive_years':3,'momentum_months':4,'momentum_threshold':.85,'reinvest_cash_reserve':0,'rank_by':'yield','momentum_periods':'','max_yield':999.0}

def make_screen(mode):
    def screen(history,as_of,min_consecutive_years=3,dividend_details_by_code=None):
        y=int(as_of[:4]); latest=y-1 if int(as_of[5:7])>=7 else y-2; out=[]
        for code,legacy in history.items():
            vals={}
            for x in (dividend_details_by_code or {}).get(code,legacy):
                ex=str(x.get('ex_date') or x.get('ex_dividend_date') or '')[:10]
                try: d=float(x.get('dps',x.get('cash_div_per_share',0)) or 0)
                except (TypeError,ValueError): d=0
                if len(ex)==10 and ex<=as_of and isinstance(x.get('year'),int) and d>0: vals[x['year']]=vals.get(x['year'],0)+d
            if not set(range(latest-2,latest+1)).issubset(vals): continue
            if mode=='latest_non_decrease' and not vals[latest]>=vals[latest-1]: continue
            if mode=='three_non_decrease' and not (vals[latest-2]<=vals[latest-1]<=vals[latest]): continue
            out.append(str(code).zfill(6))
        return sorted(out)
    return screen
def oos(nav,s):
    x=[z for z in nav if str(z.get('date',''))>=s]
    return {'observations':len(x)} if len(x)<2 else {'observations':len(x),**_compute_metrics(x,float(x[0]['nav']))}
def run(name,mode,dates,fee=1):
    old=bt.screen_dynamic_pool; bt.screen_dynamic_pool=make_screen(mode)
    try:
        rules=dict(BASE)
        if fee!=1:
            for k in ('buy_commission_rate','sell_commission_rate','stamp_duty_rate','transfer_fee_rate'): rules[k]=bt.BACKTEST_RULES.get(k,0)*fee
        r=run_backtest(rules=rules,dynamic_pool=True,manifest_path=str(MP),rebalance_dates=dates,verbose=False)
    finally: bt.screen_dynamic_pool=old
    nav=r.get('nav_series') or []
    return {'name':name,'mode':mode,'fee_multiple':fee,'rules':rules,'full':r.get('metrics') or {},'rolling36':_window_metrics(nav,36),'rolling48':_window_metrics(nav,48),'oos':{y:oos(nav,y+'-01-01') for y in ('2021','2023','2025')},'pool_provenance':r.get('pool_provenance',[])}
def main():
    d=json.loads(DP.read_text(encoding='utf-8')); dates=d.get('dates',d); m=json.loads(MP.read_text(encoding='utf-8'))
    specs=[('baseline','baseline'),('latest_non_decrease','latest_non_decrease'),('three_non_decrease','three_non_decrease')]
    rows=[run(n,mode,dates) for n,mode in specs]; best=max(rows,key=lambda z:z['full'].get('cagr',-999)); rows.append(run(best['name']+'_fee3x',best['mode'],dates,3))
    payload={'round':15,'method':'预注册分红质量窄实验；最近确认连续三年正DPS后，测试最近年度/三年DPS不下降；逐笔ex_date截断；完整账本、OOS、rolling36/48、3倍成本压力；无未来函数','base_rules':BASE,'variants':[x[0] for x in specs],'manifest_records_sha256':m.get('records_sha256'),'dates_sha256':d.get('dates_sha256'),'data_cutoff':m.get('as_of'),'dates':{'count':len(dates),'first':dates[0],'last':dates[-1]},'experiments':rows,'audit':{'future_function_check':'通过：每期仅使用 ex_date <= signal_date 的已知DPS。','data_gap':'仅将有有效除权日和正DPS的年度纳入；缺失年度不通过连续三年资格，未以未来记录补齐。'}}
    (ROOT/'data/round15_dividend_quality.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps([{'name':z['name'],'cagr':z['full'].get('cagr'),'dd':z['full'].get('max_drawdown'),'sharpe':z['full'].get('sharpe'),'r36':z['rolling36'].get('min_cagr')} for z in rows],ensure_ascii=False,indent=2))
if __name__=='__main__': main()
