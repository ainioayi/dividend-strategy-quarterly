import json
from pathlib import Path
from round4_experiments import run
def main():
 d=json.loads(Path('data/rebalance_dates_monthly.json').read_text()); dates=d['dates'] if isinstance(d,dict) else d; ex=[]
 for mt in [.85,.88,.89,.90,.91,.92,.93,.94,.95]: ex.append((f'm4_{mt}',{'momentum_months':4,'momentum_threshold':mt}))
 for ey in [7,7.2,7.5,7.8,8]: ex.append((f'ey{ey}',{'entry_yield':ey}))
 for mm in [2,6]: ex.append((f'm{mm}',{'momentum_months':mm,'momentum_threshold':.95}))
 for mt in [.90,.93,.95]:
  for ey in [7.2,7.5,7.8]: ex.append((f'combo_{mt}_{ey}',{'momentum_months':4,'momentum_threshold':mt,'entry_yield':ey}))
 rows=[run(n,c,dates,'data/universe_manifest.json') for n,c in ex]
 Path('data/round4_momentum_local.json').write_text(json.dumps({'dates':{'count':len(dates),'first':min(dates),'last':max(dates)},'experiments':rows},ensure_ascii=False,indent=2))
 print(sorted([(x['name'],x['full']['cagr'],x['full']['max_drawdown'],x['rolling36'].get('min_cagr'),x['oos2021']['cagr']) for x in rows],key=lambda z:z[1],reverse=True)[:8])
if __name__=='__main__': main()
