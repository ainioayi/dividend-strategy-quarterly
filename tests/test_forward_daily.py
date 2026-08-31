import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from forward_daily import decide_action, decide_combined_action, save_snapshot_and_report


def _calendar(*values):
    days = [date.fromisoformat(value) for value in values]
    return lambda start, end: [day for day in days if start <= day <= end]


def test_august_27_does_not_generate_august_31_signal():
    action = decide_action(
        date(2026, 8, 27), [],
        _calendar("2026-08-03", "2026-08-27", "2026-08-28", "2026-08-31"),
    )
    assert action["action"] == "noop"
    assert action["target_date"] == "2026-08-31"


def test_only_last_real_trading_day_can_signal():
    trading = _calendar("2026-08-28", "2026-08-31")
    assert decide_action(date(2026, 8, 31), [], trading)["action"] == "signal"
    with pytest.raises(RuntimeError, match="错过上月最后"):
        decide_action(date(2026, 9, 1), [], _calendar("2026-08-31", "2026-09-01", "2026-09-30"))


def test_pending_signal_only_executes_on_next_real_trading_day():
    rows = [{"event_type": "signal", "period": "2026-08", "signal_date": "2026-08-31"}]
    trading = _calendar("2026-09-01", "2026-09-02")
    action = decide_action(date(2026, 9, 1), rows, trading)
    assert action == {
        "action": "execute",
        "period": "2026-08",
        "target_date": "2026-09-01",
        "is_trading_day": True,
    }
    with pytest.raises(RuntimeError, match="错过信号后"):
        decide_action(date(2026, 9, 2), rows, trading)


def test_multiple_pending_signals_fail_closed():
    rows = [
        {"event_type": "signal", "period": "2026-08", "signal_date": "2026-08-31"},
        {"event_type": "signal", "period": "2026-09", "signal_date": "2026-09-30"},
    ]
    with pytest.raises(RuntimeError, match="多条待执行信号"):
        decide_action(date(2026, 10, 8), rows, _calendar("2026-10-08"))


def test_non_trading_day_is_exposed_for_daily_performance_gate():
    action = decide_action(
        date(2026, 8, 29), [],
        _calendar("2026-08-28", "2026-08-31"),
    )
    assert action["action"] == "noop"
    assert action["is_trading_day"] is False


def test_combined_plan_recovers_strategy_missing_current_signal():
    signal = {"event_type": "signal", "period": "2026-08", "signal_date": "2026-08-31"}
    action = decide_combined_action(
        date(2026, 8, 31),
        {"v1": [signal], "v2": [signal], "v3": [signal], "v5": [], "ma_v22": [signal]},
        _calendar("2026-08-28", "2026-08-31", "2026-09-01"),
    )
    assert action["action"] == "signal"
    assert action["strategies"]["v1"]["action"] == "noop"
    assert action["strategies"]["v5"]["action"] == "signal"


def test_combined_plan_fails_when_strategy_actions_conflict():
    pending = {"event_type": "signal", "period": "2026-08", "signal_date": "2026-08-28"}
    with pytest.raises(RuntimeError, match="门禁动作冲突"):
        decide_combined_action(
            date(2026, 8, 31),
            {"v1": [pending], "v5": []},
            _calendar("2026-08-28", "2026-08-31"),
        )


def test_snapshot_and_chinese_report_are_machine_readable(tmp_path):
    manifest = tmp_path / "manifest.json"
    dates = tmp_path / "dates.json"
    cache = tmp_path / "cache"
    journal = tmp_path / "journal.jsonl"
    output = tmp_path / "forward"
    cache.mkdir()
    manifest.write_text('{"as_of":"2026-08-27"}', encoding="utf-8")
    dates.write_text('{"as_of":"2026-08-27"}', encoding="utf-8")
    (cache / "kl_600000.json").write_text("{}", encoding="utf-8")
    journal.write_text("", encoding="utf-8")
    save_snapshot_and_report(
        "2026-08-27", {"action": "noop", "reason": "尚未到月末"},
        manifest_path=manifest, dates_path=dates, cache_dir=cache,
        journal_path=journal, output_dir=output,
    )
    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    snapshot = json.loads((output / "snapshots/2026-08-27/input_hashes.json").read_text(encoding="utf-8"))
    assert status["signal_count"] == 0
    assert snapshot["cache_files"][0]["sha256"]
    assert "高息动量 V1（2只正式）观察状态" in (output / "status.md").read_text(encoding="utf-8")
