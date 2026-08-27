from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from backtest import run_backtest, _compute_metrics
from round3_experiments import _window_metrics

MP = ROOT / 'data/universe_manifest.json'
DP = ROOT / 'data/rebalance_dates_monthly.json'
BASE = {'entry_yield': 7.5, 'hold_yield': 5.5, 'max_holdings': 2,
        'rebalance_threshold': 2.0, 'execution_lag_days': 1,
        'pool_min_consecutive_years': 3, 'momentum_months': 4,
        'momentum_threshold': .85, 'reinvest_cash_reserve': 0,
        'rank_by': 'yield', 'momentum_periods': '', 'max_yield': 999.0}

def win(nav, start):
    x = [z for z in nav if str(z.get('date', '')) >= start]
    return {'observations': len(x)} if len(x) < 2 else {'observations': len(x), **_compute_metrics(x, float(x[0]['nav']))}

def one(name, override, dates):
    rules = dict(BASE); rules.update(override)
    result = run_backtest(rules=rules, dynamic_pool=True, manifest_path=str(MP), rebalance_dates=dates, verbose=False)
    nav = result.get('nav_series') or []
    return {'name': name, 'rules': rules, 'full': result.get('metrics') or {},
            'rolling36': _window_metrics(nav, 36), 'rolling48': _window_metrics(nav, 48),
            'oos': {s: win(nav, s + '-01-01') for s in ('2021', '2023', '2025')}}

def main():
    dates_doc = json.loads(DP.read_text(encoding='utf-8')); dates = dates_doc.get('dates', dates_doc)
    manifest = json.loads(MP.read_text(encoding='utf-8'))
    variants = [(f'rebalance_{x:g}', {'rebalance_threshold': x}) for x in (1.5, 1.75, 2.25, 2.5)]
    variants += [(f'momentum_months_{x}', {'momentum_months': x}) for x in (3, 5)]
    rows = []
    for i, (name, override) in enumerate(variants, 1):
        print(f'[{i}/{len(variants)}] {name}', flush=True)
        rows.append(one(name, override, dates))
    payload = {'round': 11, 'method': '简单控制变量邻域；冻结缓存完整账本、连续切片 OOS、rolling36/48；不重新初始化账户',
               'base_rules': BASE, 'manifest_records_sha256': manifest.get('records_sha256'),
               'dates_sha256': dates_doc.get('dates_sha256'), 'data_cutoff': manifest.get('as_of'),
               'dates': {'count': len(dates), 'first': dates[0], 'last': dates[-1]}, 'experiments': rows}
    (ROOT / 'data/round11_simple_controls.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps([{'name': r['name'], 'cagr': r['full'].get('cagr'), 'dd': r['full'].get('max_drawdown'),
                       'sharpe': r['full'].get('sharpe'), 'r36': r['rolling36'].get('min_cagr'),
                       'r48': r['rolling48'].get('min_cagr'), 'oos21': r['oos']['2021'].get('cagr'),
                       'oos23': r['oos']['2023'].get('cagr'), 'oos25': r['oos']['2025'].get('cagr')} for r in rows], ensure_ascii=False, indent=2))

if __name__ == '__main__': main()
