from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from forward_performance import build_performance, update_market_snapshot


def _metadata(start: str = "2026-08-25") -> dict:
    return {
        "forward_start_date": start,
        "v1_commit": "frozen-v1",
        "rules_sha256": "rules",
        "rules": {
            "initial_capital": 100000,
            "lot_size": 100,
            "buy_commission_rate": 0,
            "min_commission": 0,
            "max_holdings": 2,
        },
    }


def _market(start: str, rows: list[tuple[str, float]], securities=None) -> dict:
    return {
        "schema_version": 1,
        "as_of": rows[-1][0],
        "price_format": "unadjusted_close",
        "benchmark": {
            "code": "510300",
            "name": "沪深300ETF华泰柏瑞",
            "prices": [
                {"date": day, "close": close, "volume_shares": 100000}
                for day, close in rows
            ],
            "dividends": [],
            "sources": {"prices": {}, "dividends": {}},
        },
        "securities": securities or {},
        "hashes": {"content_sha256": "market"},
    }


def test空账本保持十万元并展示510300基准() -> None:
    market = _market("2026-08-25", [("2026-08-25", 10), ("2026-08-26", 11)])
    result = build_performance(_metadata(), [], market)
    assert result["strategy"]["total_assets"] == 100000
    assert result["strategy"]["cumulative_return_pct"] == 0
    assert result["strategy"]["trade_count"] == 0
    assert result["benchmark"]["cumulative_return_pct"] == 10
    assert result["holdings"] == []
    assert result["transactions"] == []


def test执行后每日盯市并按除权日计入持仓分红() -> None:
    buy = {
        "date": "2026-08-26", "side": "买入", "code": "600000",
        "name": "浦发银行", "shares": 100, "price": 10, "gross": 1000,
        "fees": {"total": 0}, "net_cash": 1000, "reason": "测试买入",
    }
    journal = [{
        "event_type": "execution",
        "period": "2026-08",
        "execution_date": "2026-08-26",
        "operations": [buy],
        "cumulative_events": [buy],
        "holdings": [{"code": "600000", "shares": 100, "entry_price": 10}],
        "cash": 99000,
        "nav": 100000,
    }]
    securities = {
        "600000": {
            "name": "浦发银行",
            "prices": [
                {"date": "2026-08-26", "close": 10},
                {"date": "2026-08-27", "close": 12},
            ],
            "dividends": [{
                "year": 2025, "ex_date": "2026-08-27", "dps": 1,
                "bonus_ratio": 0, "transfer_ratio": 0,
            }],
        }
    }
    market = _market(
        "2026-08-25",
        [("2026-08-25", 10), ("2026-08-26", 10), ("2026-08-27", 10)],
        securities,
    )
    result = build_performance(_metadata(), journal, market)
    # 入场次日除权，按 20% 税率净入账 80 元；股价上涨贡献 200 元。
    assert result["strategy"]["total_assets"] == 100280
    assert result["strategy"]["dividends"] == 80
    assert result["strategy"]["trade_count"] == 1
    assert result["holdings"][0]["unrealized_pnl"] == 200
    assert result["holdings"][0]["price_date"] == "2026-08-27"
    assert result["transactions"][0]["side"] == "买入"


def test市场快照只抓取账本涉及证券并保留来源哈希() -> None:
    journal = [{
        "event_type": "execution",
        "execution_date": "2026-08-26",
        "holdings": [{"code": "600000", "shares": 100, "entry_price": 10}],
        "operations": [],
    }]

    def prices(code: str, start: str, as_of: str) -> dict:
        return {
            "code": code,
            "price_format": "unadjusted_close",
            "selected_provider": "source-a",
            "validated_providers": ["source-a", "source-b"],
            "prices": [{"date": start, "close": 10, "volume_shares": 100}],
            "prices_sha256": f"price-{code}",
        }

    def benchmark_dividends(as_of: str) -> dict:
        return {"provider": "source-c", "url": "https://example.test", "dividends": []}

    snapshot = update_market_snapshot(
        _metadata(), journal, "2026-08-25", {},
        price_fetcher=prices,
        benchmark_dividend_fetcher=benchmark_dividends,
        stock_dividend_fetcher=lambda code, as_of: [],
        sleep_seconds=0,
        names={"600000": "浦发银行"},
    )
    assert snapshot["active_codes"] == ["600000"]
    assert snapshot["securities"]["600000"]["name"] == "浦发银行"
    assert snapshot["benchmark"]["hashes"]["prices_sha256"] == "price-510300"
    assert snapshot["hashes"]["content_sha256"]


def test交易日没有510300当日收盘价时拒绝发布() -> None:
    def stale_prices(code: str, start: str, as_of: str) -> dict:
        return {
            "selected_provider": "source-a", "validated_providers": ["source-a"],
            "prices": [{"date": "2026-08-25", "close": 10, "volume_shares": 100}],
            "prices_sha256": "stale",
        }

    with pytest.raises(RuntimeError, match="尚无 2026-08-26 收盘价"):
        update_market_snapshot(
            _metadata(), [], "2026-08-26", {},
            price_fetcher=stale_prices,
            benchmark_dividend_fetcher=lambda as_of: {
                "provider": "source", "url": "https://example.test", "dividends": []
            },
            sleep_seconds=0,
            names={},
        )
