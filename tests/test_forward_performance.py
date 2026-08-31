from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from forward_performance import build_performance, build_strategy_suite, update_market_snapshot


def _metadata(start: str = "2026-08-25") -> dict:
    return {
        "forward_start_date": start,
        "v1_commit": "frozen-v1",
        "rules_sha256": "rules",
        "capital_policy": {
            "target_allocation_pct": 100,
            "cash_reserve": 0,
            "residual_cash_rule": "整数手尾差",
        },
        "observation_policy": {
            "minimum_months": 6,
            "target_months": 12,
            "parameter_changes_allowed": False,
            "v2_mode": "shadow_only",
            "v2_can_write_v1_journal": False,
        },
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


def test空账本时策略和510300都保持现金等待同步建仓() -> None:
    market = _market("2026-08-25", [("2026-08-25", 10), ("2026-08-26", 11)])
    result = build_performance(_metadata(), [], market)
    assert result["strategy"]["total_assets"] == 100000
    assert result["strategy"]["cumulative_return_pct"] == 0
    assert result["strategy"]["trade_count"] == 0
    assert result["strategy"]["target_allocation_pct"] == 100
    assert result["strategy"]["cash_reserve"] == 0
    assert result["audit"]["capital_policy"]["target_allocation_pct"] == 100
    assert result["audit"]["observation_policy"]["v2_mode"] == "shadow_only"
    assert result["benchmark"]["total_assets"] == 100000
    assert result["benchmark"]["cumulative_return_pct"] == 0
    assert result["benchmark"]["fees"] == 0
    assert result["benchmark"]["inception_date"] is None
    assert result["benchmark"]["status"] == "等待 V1 首笔模拟成交"
    assert [row["benchmark_return_pct"] for row in result["series"]] == [0, 0]
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
    assert result["benchmark"]["inception_date"] == "2026-08-26"
    assert result["benchmark"]["status"] == "与 V1 同日建仓"
    assert [row["benchmark_return_pct"] for row in result["series"]] == [0, 0, 0]


def testV5现金跨年逐交易日计息且同日先计息后入分红() -> None:
    metadata = _metadata("2024-12-30")
    metadata.update({"version": "V5", "shadow": True})
    metadata["rules"]["initial_capital"] = 2000
    buy = {
        "date": "2024-12-30", "side": "买入", "code": "600000",
        "shares": 100, "price": 10, "gross": 1000, "fees": {"total": 0},
    }
    journal = [{
        "event_type": "execution",
        "period": "2024-12",
        "execution_date": "2024-12-30",
        "operations": [buy],
        "cumulative_events": [buy],
        "holdings": [{"code": "600000", "shares": 100, "entry_price": 10}],
        "cash": 1000,
        "nav": 2000,
    }]
    securities = {
        "600000": {
            "name": "浦发银行",
            "prices": [
                {"date": "2024-12-30", "close": 10},
                {"date": "2024-12-31", "close": 10},
                {"date": "2025-01-02", "close": 10},
            ],
            "dividends": [{
                "year": 2024, "ex_date": "2024-12-31", "dps": 1,
                "bonus_ratio": 0, "transfer_ratio": 0,
            }],
        }
    }
    market = _market(
        "2024-12-30",
        [("2024-12-30", 10), ("2024-12-31", 10), ("2025-01-02", 10)],
        securities,
    )

    result = build_performance(metadata, journal, market)

    cash_after_dividend = 1000 * (1 + 0.019 / 252) + 80
    expected_cash = cash_after_dividend * (1 + 0.014 / 252)
    assert result["series"][1]["strategy_nav"] == pytest.approx(cash_after_dividend + 1000, abs=0.01)
    assert result["series"][2]["strategy_nav"] == pytest.approx(expected_cash + 1000, abs=0.01)
    assert result["strategy"]["cash"] == pytest.approx(expected_cash, abs=0.01)
    assert result["strategy"]["dividends"] == 80


def test510300从V1首笔模拟成交日开始计算收益() -> None:
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
                {"date": "2026-08-27", "close": 10},
            ],
            "dividends": [],
        }
    }
    market = _market(
        "2026-08-25",
        [("2026-08-25", 10), ("2026-08-26", 11), ("2026-08-27", 12)],
        securities,
    )

    result = build_performance(_metadata(), journal, market)

    assert [row["benchmark_return_pct"] for row in result["series"]] == [0, 0, 9]
    assert result["benchmark"]["cumulative_return_pct"] == 9


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


def test组合业绩同时保留五套独立账户和共同基准() -> None:
    metadatas = {
        "v1": _metadata(), "v2": _metadata(), "v3": _metadata(),
        "v5": _metadata(), "ma_v22": _metadata(),
    }
    metadatas["v2"].update({"version": "V2", "shadow": True, "rules_sha256": "v2"})
    metadatas["v2"]["rules"]["max_holdings"] = 3
    metadatas["v3"].update({"version": "V3", "shadow": True, "rules_sha256": "v3"})
    metadatas["v3"]["rules"]["max_holdings"] = 4
    metadatas["v5"].update({"version": "V5", "shadow": True, "rules_sha256": "v5"})
    metadatas["v5"]["rules"]["max_holdings"] = 6
    metadatas["ma_v22"].update({
        "version": "V2.2", "strategy_id": "ma_v22", "shadow": True,
        "strategy": "多资产风险预算 V2.2（全球版影子）", "rules_sha256": "ma_v22",
    })
    metadatas["ma_v22"]["rules"]["max_holdings"] = 4
    journals = {key: [] for key in metadatas}
    market = _market("2026-08-25", [("2026-08-25", 10), ("2026-08-26", 11)])
    health = {
        "strategies": {
            "v1": {"status": "正常", "outcome": "success"},
            "v2": {"status": "失败，未冒充更新成功", "outcome": "failure"},
            "v3": {"status": "正常", "outcome": "success"},
            "v5": {"status": "正常", "outcome": "success"},
            "ma_v22": {"status": "正常", "outcome": "success"},
        }
    }

    result = build_strategy_suite(metadatas, journals, market, health)

    assert result["schema_version"] == 4
    assert list(result["strategies"]) == ["v1", "v2", "v3", "v5", "ma_v22"]
    assert [result["strategies"][key]["summary"]["max_holdings"] for key in result["strategies"]] == [2, 3, 4, 6, 4]
    assert result["strategies"]["v2"]["health"]["outcome"] == "failure"
    assert result["strategies"]["v1"]["audit"]["journal_sha256"] == result["audit"]["strategy_journals"]["v1"]
    assert all(row["v1_return_pct"] == row["v2_return_pct"] == row["v3_return_pct"] == row["v5_return_pct"] == row["ma_v22_return_pct"] == 0 for row in result["series"])
    assert all(row["v5_nav"] == 100000 for row in result["series"])
    assert result["strategy"]["total_assets"] == result["strategies"]["v1"]["summary"]["total_assets"]
    assert result["holdings"] == result["strategies"]["v1"]["holdings"]


def test公开页面声明并渲染五策略契约() -> None:
    html = (Path(__file__).resolve().parents[1] / "site" / "index.html").read_text(encoding="utf-8")
    assert "const strategyIds = Object.keys(strategyLabels)" in html
    assert "v5: { name: '高息动量 V5（附件规则影子）'" in html
    assert "ma_v22: { name: '多资产风险预算 V2.2（全球版影子）'" in html
    assert "['v1', 'v2', 'v3', 'v5', 'ma_v22'].includes(query.get('strategy'))" in html
    assert "data.schema_version !== 4" in html
    assert "`${state.strategy}_nav`" in html


def testV22基金分红按到账日入账且不扣股票红利税() -> None:
    metadata = _metadata()
    metadata.update({
        "version": "V2.2", "strategy_id": "ma_v22", "shadow": True,
        "strategy": "多资产风险预算 V2.2（全球版影子）",
    })
    buy = {
        "date": "2026-08-26", "side": "买入", "code": "518880",
        "shares": 100, "price": 10, "gross": 1000, "fees": {"total": 0},
    }
    journal = [{
        "event_type": "execution", "period": "2026-08", "execution_date": "2026-08-26",
        "operations": [buy], "cumulative_events": [buy],
        "holdings": [{"code": "518880", "shares": 100, "entry_price": 10}],
        "cash": 99000, "nav": 100000,
    }]
    securities = {"518880": {
        "name": "黄金 ETF",
        "prices": [
            {"date": "2026-08-26", "close": 10},
            {"date": "2026-08-27", "close": 10},
            {"date": "2026-08-28", "close": 10},
        ],
        "dividends": [{
            "record_date": "2026-08-27", "ex_date": "2026-08-28",
            "pay_date": "2026-08-28", "cash_per_unit": 1,
        }],
    }}
    market = _market("2026-08-25", [
        ("2026-08-25", 10), ("2026-08-26", 10),
        ("2026-08-27", 10), ("2026-08-28", 10),
    ], securities)

    result = build_performance(metadata, journal, market)

    assert result["strategy"]["name"] == "多资产风险预算 V2.2（全球版影子）"
    assert result["strategy"]["cash"] == 99100
    assert result["strategy"]["dividends"] == 100
