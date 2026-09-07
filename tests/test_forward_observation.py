from datetime import date

import pytest

from forward_observation import PLAN, _add_months, build_observation


def _performance(as_of="2026-09-30", quality="passed"):
    dates = ["2026-09-01", "2026-09-02", as_of]
    navs = [100000, 90000, 110000]
    strategies = {}
    for strategy_id in PLAN["strategy_ids"]:
        strategies[strategy_id] = {
            "summary": {"name": strategy_id},
            "series": [{"date": day, "nav": nav, "return_pct": 0} for day, nav in zip(dates, navs)],
            "transactions": [
                {"date": "2026-09-01", "side": "买入", "gross": 50000, "fees": 5},
                {"date": "2026-10-01", "side": "卖出", "gross": 99999, "fees": 99},
                {"date": "2026-09-15", "side": "分红", "gross": 100, "fees": 0},
            ],
        }
    return {
        "schema_version": 4,
        "as_of": as_of,
        "benchmark": {"inception_date": "2026-09-01"},
        "data_quality": {"status": quality},
        "series": [
            {"date": day, "benchmark_nav": nav}
            for day, nav in zip(dates, [100000, 95000, 105000])
        ],
        "strategies": strategies,
    }


def test_build_observation_固定口径且忽略未来交易():
    result = build_observation(_performance())
    v1 = result["strategies"]["v1"]

    assert result["plan"]["registration"] == "观察已开始后制定，不能宣称前瞻预注册"
    assert len(result["plan_sha256"]) == 64
    assert v1["return_pct"] == 10
    assert v1["max_drawdown_pct"] == 10
    assert v1["two_way_turnover_pct"] == pytest.approx(50.0, abs=0.01)
    assert v1["fees"] == 5
    assert v1["excess_return_pct"] == 5
    assert result["conclusion"]["status"] == "insufficient_observation"
    assert result["conclusion"]["automatic_switch"] is False
    month = result["monthly_records"][0]["strategies"]["v1"]
    assert month["return_pct"] == 10
    assert month["max_drawdown_pct"] == 10
    assert month["fees"] == 5
    assert month["excess_return_pct"] == 5


def test_月末边界与未完成月份明确():
    complete = build_observation(_performance("2026-09-30"))
    incomplete = build_observation(_performance("2026-09-29"))

    assert complete["monthly_records"][0]["is_complete"] is True
    assert incomplete["monthly_records"][0]["is_complete"] is False
    assert _add_months(date(2024, 8, 31), 6) == date(2025, 2, 28)


def test_数据质量异常阻止结论():
    result = build_observation(_performance(quality="failed"))

    assert result["conclusion"]["status"] == "data_quality_blocked"
    assert result["data_quality"]["allows_conclusion"] is False


def test_六个月检查点仅要求人工复核():
    result = build_observation(_performance("2027-03-01"))

    assert result["checkpoints"][0]["status"] == "ready_for_manual_review"
    assert result["conclusion"]["status"] == "manual_review_required"
    assert result["conclusion"]["ranking_allowed"] is False


def test_检查点等待到期后首个实际估值日():
    before = build_observation(_performance("2027-03-01"))
    payload = _performance("2027-03-02")
    payload["series"][-1]["date"] = "2027-03-02"
    for strategy in payload["strategies"].values():
        strategy["series"][-1]["date"] = "2027-03-02"
    after = build_observation(payload)

    assert before["checkpoints"][0]["review_date"] == "2027-03-01"
    assert after["checkpoints"][0]["review_date"] == "2027-03-02"


def test_首日手续费计入收益和回撤():
    payload = _performance()
    payload["strategies"]["v1"]["series"][0]["nav"] = 99000
    result = build_observation(payload)

    assert result["strategies"]["v1"]["return_pct"] == 10
    assert result["strategies"]["v1"]["max_drawdown_pct"] == 10


def test_空账本等待执行而不报错():
    payload = _performance()
    payload["benchmark"]["inception_date"] = None
    for strategy in payload["strategies"].values():
        strategy["summary"]["initial_capital"] = 100000
        strategy["series"] = []
        strategy["transactions"] = []
    result = build_observation(payload)

    assert result["period"]["start_date"] is None
    assert result["conclusion"]["status"] == "awaiting_execution"
    assert all(row["due_date"] is None and row["status"] == "pending" for row in result["checkpoints"])
    assert result["monthly_records"] == []
