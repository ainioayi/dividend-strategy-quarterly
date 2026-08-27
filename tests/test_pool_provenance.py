import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from backtest import run_backtest


def test_pool_provenance_is_deterministic_and_backward_compatible():
    result = run_backtest(
        rules={'initial_capital': 100000, 'pool_mode': 'dynamic', 'momentum_months': 0,
               'execution_lag_days': 0},
        dynamic_pool=True,
        codes=['000333', '600036'],
        rebalance_dates=['2020-01-31'],
        verbose=False,
    )
    rows = result['pool_provenance']
    assert len(rows) == 1
    row = rows[0]
    assert row['signal_date'] == '2020-01-31'
    assert row['pool_count'] >= 0
    assert len(row['pool_codes_sha256']) == 64
    assert row['pool_codes_sha256'] == hashlib.sha256(b'').hexdigest() or row['pool_count'] > 0
    assert row['execution_date'] in ('2020-01-31', None)
    assert 'dynamic_pool' in result
    assert 'metrics' in result
    assert 'listing_windows' not in result


def test_pool_provenance_keeps_execution_gap_when_no_price_rows():
    """无可用行情时也保留信号日和执行缺口记录。"""
    result = run_backtest(
        rules={'pool_mode': 'dynamic', 'momentum_months': 0,
               'execution_lag_days': 1},
        dynamic_pool=True,
        codes=['000333'],
        rebalance_dates=['2099-01-31'],
        verbose=False,
    )
    assert result['pool_provenance'] == [{
        'signal_date': '2099-01-31',
        'pool_count': 0,
        'pool_codes_sha256': hashlib.sha256(b'').hexdigest(),
        'execution_date': None,
    }]


def test_explicit_through_date_bounds_fixed_pool_replay():
    """固定池显式截止日时，不应把缓存中的后续交易日用于执行。"""
    result = run_backtest(
        rules={'through_date': '2020-01-31', 'momentum_months': 0,
               'execution_lag_days': 1},
        dynamic_pool=False,
        codes=['000333'],
        rebalance_dates=['2020-01-31', '2020-02-29'],
        verbose=False,
    )
    assert result['data_cutoff'] == '2020-01-31'
    assert result['rebalance_dates']['last'] == '2020-01-31'
    assert result['pool_provenance'][-1]['execution_date'] is None


def test_rebalance_dates_metadata_uses_portable_path():
    """结果元数据不应写入当前机器的绝对日期文件路径。"""
    result = run_backtest(
        rules={'pool_mode': 'curated', 'momentum_months': 0,
               'execution_lag_days': 0},
        dynamic_pool=False,
        codes=['000333'],
        rebalance_dates_path='data/rebalance_dates_monthly.json',
        verbose=False,
    )
    assert result['rebalance_dates']['path'] == 'data/rebalance_dates_monthly.json'


def test_listing_window_excludes_stock_after_delisting():
    result = run_backtest(
        rules={'pool_mode': 'curated', 'momentum_months': 0, 'execution_lag_days': 0},
        dynamic_pool=False,
        codes=['000333'],
        rebalance_dates=['2020-01-31'],
        listing_windows={'000333': {'list_date': '1996-08-30', 'delist_date': '2019-12-31'}},
        verbose=False,
    )
    assert result['pool_provenance'][0]['pool_count'] == 0
    assert result['listing_windows']['count'] == 1


def test_listing_window_rejects_inverted_dates():
    import pytest
    with pytest.raises(ValueError, match='退市日早于上市日'):
        run_backtest(
            codes=['000333'], rebalance_dates=['2020-01-31'], verbose=False,
            listing_windows={'000333': {'list_date': '2020-01-01', 'delist_date': '2019-01-01'}},
        )


def test_delisting_recovery_rate_is_bounded():
    import pytest
    with pytest.raises(ValueError, match='0 到 1'):
        run_backtest(codes=['000333'], rebalance_dates=['2020-01-31'], verbose=False,
                     delisting_recovery_rate=1.1)


def test_delisting_uses_last_tradable_price_without_reusing_it_as_trade_price():
    from backtest import _find_last_tradable_price, _find_price

    prices = {"2023-05-09": 0.4}
    assert _find_price(prices, "2023-06-06") is None
    assert _find_last_tradable_price(prices, "2023-06-06") == (0.4, "2023-05-09")
