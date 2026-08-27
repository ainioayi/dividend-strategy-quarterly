import json
from pathlib import Path
from round4_experiments import run
def main():
 d=json.loads(Path('data/rebalance_dates_monthly.json').read_text()); dates=d['dates'] if isinstance(d,dict) else d; rows=[]
 for reserve in [0,2000,3000,4000,5000,6000]:
  for mt in [.85,.88,.90,.92,.95]: rows.append(run(f'r{reserve}_m{mt}',{'reinvest_cash_reserve':reserve,'momentum_months':4,'momentum_threshold':mt},dates,'data/universe_manifest.json'))
 out={'dates':{'count':len(dates),'first':min(dates),'last':max(dates)},'experiments':rows}; Path('data/round5_reserve_momentum.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)); print(sorted([(x['name'],x['full']['cagr'],x['full']['max_drawdown'],x['rolling36']['min_cagr'],x['oos2021']['cagr']) for x in rows],key=lambda z:z[1],reverse=True)[:10])
if __name__=='__main__': main()
