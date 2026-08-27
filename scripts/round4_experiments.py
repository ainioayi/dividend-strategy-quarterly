import json, hashlib, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
from backtest import run_backtest
import round3_experiments as r3

def run(name, change, dates, manifest):
    base={'entry_yield':7.5,'hold_yield':5.5,'momentum_months':4,'momentum_threshold':.95,'max_holdings':2,'rebalance_threshold':2.0,'execution_lag_days':1}
    base.update(change); full=run_backtest(rules=base,dynamic_pool=True,manifest_path=manifest,rebalance_dates=dates,verbose=False)
    out={'name':name,'rules':base,'full':full['metrics'],'rolling36':r3._window_metrics(full['nav_series'],36),'rolling48':r3._window_metrics(full['nav_series'],48)}
    for label,start in [('oos2021','2021-01-01'),('oos2023','2023-01-01'),('oos2025','2025-01-01')]:
        out[label]=r3._continuous_metrics(full['nav_series'],start)
    return out

def main():
    manifest='data/universe_manifest.json'; dp=json.loads(Path('data/rebalance_dates_monthly.json').read_text()); dates=dp['dates'] if isinstance(dp,dict) else dp
    ex=[('base',{})]
    for x in [6,6.5,7,7.2,7.4,7.6,8,8.5]: ex.append((f'ey{x}',{'entry_yield':x}))
    for x in [4.5,5,6,6.5]: ex.append((f'hy{x}',{'hold_yield':x}))
    for mm in [0,3,4,5]:
      for mt in [.90,.93,.95,.98,1.0]: ex.append((f'm{mm}_{mt}',{'momentum_months':mm,'momentum_threshold':mt}))
    for x in [1,3,4]: ex.append((f'mh{x}',{'max_holdings':x}))
    for x in [1,1.4,1.8,2.5]: ex.append((f'rt{x}',{'rebalance_threshold':x}))
    rows=[run(n,c,dates,manifest) for n,c in ex]
    md=json.loads(Path(manifest).read_text()); mh=md.get('records_sha256'); dh=dp.get('dates_sha256') if isinstance(dp,dict) else None
    payload={'manifest_records_sha256':mh,'dates_sha256':dh,'dates':{'count':len(dates),'first':min(dates),'last':max(dates)},'experiments':rows}
    Path('data/round4_experiments_fixed.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    print('done',len(rows),mh,dh)
if __name__=='__main__': main()
