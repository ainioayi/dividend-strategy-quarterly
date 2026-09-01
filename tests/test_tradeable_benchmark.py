from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from tradeable_benchmark import _market_symbol, parse_dividends, parse_prices, simulate_total_return


def test沪市ETF代码不会误路由到深市() -> None:
    assert _market_symbol("510300") == "sh510300"
    assert _market_symbol("sh510880") == "sh510880"


def test解析ETF未复权行情和分红() -> None:
    prices = parse_prices([
        {"day": "2026-01-20", "close": "2", "volume": "100"},
        {"day": "2026-01-21", "close": "1.9", "volume": "120"},
        {"day": "2026-01-22", "close": "2.1", "volume": "130"},
    ], "2026-01-21")
    assert [row["date"] for row in prices] == ["2026-01-20", "2026-01-21"]
    html = """
    <table class="w782 comm cfxq"><tbody><tr>
      <td>2026年</td><td>2026-01-20</td><td>2026-01-21</td>
      <td>每10份派现金1.4300元</td><td>2026-01-26</td>
    </tr></tbody></table>
    """
    records = parse_dividends(html, "2026-01-31")
    assert records[0]["cash_per_unit"] == 0.143
    assert parse_dividends(html, "2026-01-01") == []


def test基金页面明确暂无分红时返回空列表() -> None:
    html = """
    <table class="w782 comm cfxq"><tbody>
      <tr><th>年份</th><th>权益登记日</th><th>除息日</th><th>每10份分红</th><th>分红发放日</th></tr>
      <tr><td colspan="5">暂无分红信息!</td></tr>
    </tbody></table>
    """
    assert parse_dividends(html, "2026-09-01") == []


def test基金分红页面结构异常时拒绝静默返回空列表() -> None:
    with pytest.raises(ValueError, match="未识别出分红记录或明确的暂无分红状态"):
        parse_dividends("<html><body>上游返回了非预期页面</body></html>", "2026-09-01")


def test基准按整手买入并在分红到账后复投() -> None:
    payload = {
        "symbol": "510880",
        "name": "测试ETF",
        "hashes": {"prices_sha256": "p", "dividends_sha256": "d"},
        "prices": [
            {"date": "2026-01-30", "close": 10.0, "volume_shares": 10000},
            {"date": "2026-02-02", "close": 10.0, "volume_shares": 10000},
            {"date": "2026-02-03", "close": 10.0, "volume_shares": 10000},
            {"date": "2026-02-04", "close": 10.0, "volume_shares": 10000},
            {"date": "2026-02-05", "close": 10.0, "volume_shares": 10000},
            {"date": "2026-02-27", "close": 11.0, "volume_shares": 10000},
        ],
        "dividends": [{
            "record_date": "2026-02-02",
            "ex_date": "2026-02-03",
            "pay_date": "2026-02-04",
            "cash_per_unit": 1.1,
        }],
    }
    result = simulate_total_return(
        payload,
        ["2026-01-30", "2026-02-27"],
        initial_capital=100000,
        signal_start="2026-01-30",
    )
    assert result["method"]["first_trade_date"] == "2026-02-02"
    assert result["metrics"]["trade_count"] == 2
    assert result["total_dividend_cash"] > 0
    assert any(event["side"] == "分红复投" for event in result["events"])
