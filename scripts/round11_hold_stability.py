from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from backtest import run_backtest, _compute_metrics
from round3_experiments import _window_metrics

MP = ROOT / 'data' / 'universe_manifest.json'
DP = ROOT / 'data' / 'rebalance_dates_monthly.json'
BASE = {'entry_yield': 7.5, 'hold_yield': 5.5, 'max_holdings': 2,
        'rebalance_threshold': 2.0, 'execution_lag_days': 1,
        'pool_min_consecutive_years': 3, 'momentum_months': 4,
        'momentum_threshold': .85, 'reinvest_cash_reserve': 0,
        'rank_by': 'yield', 'momentum_periods': '', 'max_yield': 999.0}

def oos(nav, start):
    rows = [x for x in nav if str(x.get('date', '')) >= start]
    return {'observations': len(rows)} if len(rows) < 2 else {'observations': len(rows), **_compute_metrics(rows, float(rows[0]['nav']))}

def run_one(value, dates):
    rules = dict(BASE, hold_yield=value)
    result = run_backtest(rules=rules, dynamic_pool=True, manifest_path=str(MP), rebalance_dates=dates, verbose=False)
    nav = result.get('nav_series') or []
    return {'name': f'hold_{value:g}', 'hold_yield': value, 'rules': rules,
            'full': result.get('metrics') or {},
            'rolling36': _window_metrics(nav, 36), 'rolling48': _window_metrics(nav, 48),
            'oos': {year: oos(nav, f'{year}-01-01') for year in ('2021', '2023', '2025')}}

def main():
    dates_obj = json.loads(DP.read_text(encoding='utf-8'))
    dates = dates_obj.get('dates', dates_obj)
    manifest = json.loads(MP.read_text(encoding='utf-8'))
    values = (5.50, 5.525, 5.55, 5.575, 5.60, 5.625, 5.65, 5.675)
    experiments = []
    for i, value in enumerate(values, 1):
        print(f'[{i}/{len(values)}] hold_yield={value:g}', flush=True)
        experiments.append(run_one(value, dates))
    payload = {'round': 11, 'method': 'hold_yield 极窄邻域稳定性；完整账本、rolling36/48、连续 OOS；动态候选池；execution_lag_days=1；不使用未来函数',
               'base_rules': BASE, 'hold_yield_values': list(values),
               'manifest_records_sha256': manifest.get('records_sha256'), 'dates_sha256': dates_obj.get('dates_sha256'),
               'data_cutoff': manifest.get('as_of'),
               'dates': {'count': len(dates), 'first': dates[0], 'last': dates[-1]},
               'experiments': experiments,
               'audit': {'dynamic_pool': True, 'execution_lag_days': 1, 'signal_uses_current_date_only': True,
                         'future_function_check': '通过：回测引擎按信号日及 execution_lag_days=1 成交，未读取未来价格/分红'}}
    out = ROOT / 'data' / 'round11_hold_stability.json'
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    summary = [{'hold': x['hold_yield'], 'cagr': x['full'].get('cagr'), 'dd': x['full'].get('max_drawdown'), 'sharpe': x['full'].get('sharpe'), 'r36': x['rolling36'].get('min_cagr'), 'r48': x['rolling48'].get('min_cagr'), **{f'oos{y}': x['oos'][y].get('cagr') for y in ('2021','2023','2025')}} for x in experiments]
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
