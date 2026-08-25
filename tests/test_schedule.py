"""季度任务门禁和幂等测试。"""
from datetime import date

from schedule import gate, target_period


def test_target_period_crosses_year_boundary():
    assert target_period(date(2026, 1, 3)) == ("2025Q4", date(2025, 12, 31))
    assert target_period(date(2026, 7, 1)) == ("2026Q2", date(2026, 6, 30))


def test_gate_only_opens_after_quarter_and_inside_window():
    waiting = gate(date(2026, 4, 1), date(2026, 3, 31), None)
    assert waiting["due"] == "false"
    assert waiting["reason"] == "季度后首个交易日尚未收盘"

    ready = gate(date(2026, 4, 2), date(2026, 4, 2), None)
    assert ready == {
        "due": "true",
        "period": "2026Q1",
        "data_date": "2026-04-02",
        "reason": "可以更新",
    }

    outside = gate(date(2026, 4, 11), date(2026, 4, 11), None)
    assert outside["due"] == "false"
    assert outside["reason"] == "不在季度更新窗口"


def test_gate_is_idempotent_unless_forced():
    duplicate = gate(date(2026, 7, 2), date(2026, 7, 2), "2026Q2")
    assert duplicate["due"] == "false"
    assert duplicate["reason"] == "本季度已生成"

    forced = gate(date(2026, 8, 20), date(2026, 8, 20), "2026Q2", force=True)
    assert forced["due"] == "true"
    assert forced["period"] == "2026Q2"
