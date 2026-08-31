import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test首期隔离演练覆盖五策略且不修改正式账本():
    report = json.loads(
        (ROOT / "data" / "forward_first_cycle_rehearsal.json").read_text(encoding="utf-8")
    )
    assert report["schema_version"] == 4
    assert report["status"] == "通过"
    assert report["production_journals_unchanged"] is True
    assert list(report["strategies"]) == ["v1", "v2", "v3", "v5", "ma_v22"]
    assert [report["strategies"][key]["max_holdings"] for key in report["strategies"]] == [2, 3, 4, 6, 4]
    assert all(
        report["strategies"][key]["isolated_journal"]["signal_count"] == 1
        and report["strategies"][key]["isolated_journal"]["execution_count"] == 1
        for key in report["strategies"]
    )
    assert all(report["strategies"][key]["execution"]["residual_cash_below_one_lot"] is True
               for key in ("v1", "v2", "v3"))
    assert report["strategies"]["v5"]["execution"]["residual_cash_below_one_lot"] is False
    assert report["strategies"]["ma_v22"]["execution"]["holding_count"] >= 1
