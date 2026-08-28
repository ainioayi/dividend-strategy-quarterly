from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_forward_workflow_has_retry_freeze_gate_and_issue_alert() -> None:
    workflow = (ROOT / ".github/workflows/monthly-forward.yml").read_text(encoding="utf-8")
    assert 'cron: "30 10 * * 1-5"' in workflow
    assert 'cron: "30 12 * * 1-5"' in workflow
    assert "python scripts/verify_v1_freeze.py" in workflow
    assert "python scripts/monthly_forward.py verify" in workflow
    assert "python scripts/rehearse_forward_cycle.py" in workflow
    assert "options: [auto, signal, execute, rehearsal]" in workflow
    assert "if: inputs.mode != 'rehearsal'" in workflow
    assert 'if [[ "$RUN_MODE" == "rehearsal" ]]' in workflow
    assert "[自动告警] V1 前向更新失败" in workflow
    assert "gh issue close" in workflow


def test_v2_is_limited_to_shadow_output() -> None:
    metadata = (ROOT / "data/forward/v1_metadata.json").read_text(encoding="utf-8")
    assert '"v2_mode": "shadow_only"' in metadata
    assert '"v2_output_root": "data/forward/shadow"' in metadata
    assert '"v2_can_write_v1_journal": false' in metadata
