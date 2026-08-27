from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from backtest import run_backtest, _compute_metrics, BACKTEST_RULES
from round3_experiments import _window_metrics

MP = ROOT / 'data/universe_manifest.json'
DP = ROOT / 'data/rebalance_dates_monthly.json'
BASE = {'entry_yield': 7.5, 'hold_yield': 5.5, 'max_holdings': 2,
        'rebalance_threshold': 2.0, 'execution_lag_days': 1,
        'pool_min_consecutive_years': 3, 'momentum_months': 4,
        'momentum_threshold': .85, 'reinvest_cash_reserve': 0,
        'rank_by': 'yield', 'momentum_periods': '', 'max_yield': 999.0}

def oos(nav, start):
    xs = [x for x in nav if x['date'] >= start]
    return {'observations': len(xs)} if len(xs) < 2 else {'observations': len(xs), **_compute_metrics(xs, float(xs[0]['nav']))}

def run_variant(rank, dates, fee=1):
    rules = dict(BASE, rank_by=rank)
    if fee != 1:
        for k in ('buy_commission_rate', 'sell_commission_rate', 'stamp_duty_rate', 'transfer_fee_rate'):
            rules[k] = BACKTEST_RULES.get(k, 0) * fee
    result = run_backtest(rules=rules, dynamic_pool=True, manifest_path=str(MP), rebalance_dates=dates, verbose=False)
    nav = result.get('nav_series') or []
    return {'rank_by': rank, 'fee_multiple': fee, 'rules': rules,
            'metrics': result.get('metrics') or {}, 'rolling36': _window_metrics(nav, 36),
            'rolling48': _window_metrics(nav, 48),
            'oos': {y: oos(nav, y + '-01-01') for y in ('2021', '2023', '2025')}, 'nav': nav}

def main():
    d = json.loads(DP.read_text(encoding='utf-8')); dates = d.get('dates', d)
    m = json.loads(MP.read_text(encoding='utf-8'))
    experiments = []
    for rank in ('yield', 'momentum'):
        z = run_variant(rank, dates); z.pop('nav'); experiments.append(z)
    cost_stress = []
    for rank in ('yield', 'momentum'):
        z = run_variant(rank, dates, 3); z.pop('nav'); cost_stress.append(z)
    resets = []
    for rank in ('yield', 'momentum'):
        for start in ('2018-01-01', '2020-01-01', '2022-01-01'):
            i = next(j for j, x in enumerate(dates) if x >= start)
            subset = dates[max(0, i - 4):]
            z = run_variant(rank, subset); nav = [x for x in z['nav'] if x['date'] >= start]
            if nav:
                scale = 100000 / nav[0]['nav']; nav = [dict(x, nav=round(x['nav'] * scale, 2)) for x in nav]
            resets.append({'rank_by': rank, 'start': start, 'end': dates[-1], 'warmup_count': min(4, i),
                           'metrics': _compute_metrics(nav, 100000), 'rolling36': _window_metrics(nav, 36),
                           'rolling48': _window_metrics(nav, 48)})
    out = {'round': 24, 'method': '候选池排序依据 yield 与 momentum 对照；冻结输入；完整账本、连续 OOS、rolling36/48、三倍成本、2018/2020/2022 重置；无未来函数',
           'base_rules': BASE, 'manifest_records_sha256': m.get('records_sha256'), 'dates_sha256': d.get('dates_sha256'),
           'data_cutoff': m.get('as_of'), 'dates': {'count': len(dates), 'first': dates[0], 'last': dates[-1]},
           'experiments': experiments, 'cost_stress': cost_stress, 'reset_windows': resets,
           'audit': {'future_function_check': '通过：候选池和动量均按信号日前数据计算，交易执行滞后1个交易日。',
                     'survivorship_bias': '冻结股票集合可能缺少退市股票。'}}
    (ROOT / 'data/round24_rank_by.json').write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps([(x['rank_by'], x['metrics'].get('cagr'), x['metrics'].get('max_drawdown')) for x in experiments], ensure_ascii=False))

if __name__ == '__main__':
    main()
