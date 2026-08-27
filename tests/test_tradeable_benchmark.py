from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from tradeable_benchmark import parse_dividends, parse_prices, simulate_total_return


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
