from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_forward_workflow_has_retry_freeze_gate_and_issue_alert() -> None:
    workflow = (ROOT / ".github/workflows/monthly-forward.yml").read_text(encoding="utf-8")
    assert "  push:\n    branches: [main]\n" in workflow
    for generated_path in (
        "data/forward/**",
        "data/v5_inputs.json",
        "data/ma_v22_inputs.json",
        "site/performance.json",
    ):
        assert f'      - "{generated_path}"' in workflow
    assert 'cron: "30 10 * * 1-5"' in workflow
    assert 'cron: "30 12 * * 1-5"' in workflow
    assert "cancel-in-progress: true" in workflow
    assert "python scripts/verify_v1_freeze.py" in workflow
    assert "python scripts/monthly_forward.py verify" in workflow
    assert "python scripts/rehearse_forward_cycle.py" in workflow
    assert "python scripts/monthly_forward.py --strategy ma_v22 verify" in workflow
    assert "python scripts/refresh_ma_v22_inputs.py" in workflow
    assert "--strategy ma_v22" in workflow
    assert "MA_V22_OUTCOME" in workflow
    assert "options: [auto, signal, execute, rehearsal]" in workflow
    assert "if: inputs.mode != 'rehearsal'" in workflow
    assert 'if [[ "$RUN_MODE" == "rehearsal" ]]' in workflow
    assert "id: v5_nav\n        continue-on-error: true" in workflow
    assert "id: v5_inputs\n        continue-on-error: true" in workflow
    assert "steps.v5_nav.outcome == 'failure' || steps.v5_inputs.outcome == 'failure'" in workflow
    assert "[自动告警] 五策略前向更新失败" in workflow
    assert "gh issue close" in workflow
    assert "timeout 25m python scripts/refresh_backtest_cache.py" in workflow
    assert "timeout 15m python scripts/refresh_v5_inputs.py" in workflow
    assert "--output data/forward/v5_inputs.json" in workflow
    assert "--output data/forward/ma_v22_inputs.json" in workflow
    assert "git fetch origin main" in workflow
    assert "git rebase origin/main" in workflow
    assert "if: success() && steps.plan.outputs.is_trading_day == 'true'" in workflow
    assert "git add data/forward site/performance.json" in workflow
    assert "git add data/forward data/v5_inputs.json" not in workflow


def test_v2_is_limited_to_shadow_output() -> None:
    metadata = (ROOT / "data/forward/v1_metadata.json").read_text(encoding="utf-8")
    assert '"v2_mode": "shadow_only"' in metadata
    assert '"v2_output_root": "data/forward/shadow"' in metadata
    assert '"v2_can_write_v1_journal": false' in metadata
