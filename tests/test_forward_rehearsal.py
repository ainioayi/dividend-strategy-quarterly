from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rehearse_forward_cycle import run_rehearsal


def test正式210只缓存可以隔离预演首期信号执行和公开业绩() -> None:
    report = run_rehearsal()

    assert report["status"] == "通过"
    assert report["code_count"] == 210
    assert report["signal"]["plan"] == "signal"
    assert report["signal"]["eligible_entry_count"] > 0
    assert report["execution"]["plan"] == "execute"
    assert report["execution"]["operation_count"] > 0
    assert report["execution"]["holding_count"] == report["signal"]["eligible_entry_count"]
    assert report["execution"]["residual_cash_below_one_lot"] is True
    assert report["execution"]["cash"] < report["execution"]["minimum_next_lot_cost"]
    assert report["public_performance"]["benchmark_inception_date"] == "2026-09-01"
    assert report["isolated_journal"] == {"signal_count": 1, "execution_count": 1}
    assert report["production_journal_unchanged"] is True
