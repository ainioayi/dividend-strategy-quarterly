from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_manual_forward_workflow_keeps_research_gates_without_overwriting_public_page() -> None:
    workflow = (ROOT / ".github/workflows/monthly-forward.yml").read_text(encoding="utf-8")
    assert "  workflow_dispatch:" in workflow
    assert "  push:" not in workflow
    assert "  schedule:" not in workflow
    assert "cancel-in-progress: true" in workflow
    assert "python scripts/verify_v1_freeze.py" in workflow
    assert "python scripts/monthly_forward.py verify" in workflow
    assert "python scripts/rehearse_forward_cycle.py" in workflow
    assert "python scripts/monthly_forward.py --strategy ma_v22 verify" in workflow
    assert "python scripts/refresh_ma_v22_inputs.py" in workflow
    assert "--strategy ma_v22" in workflow
    assert "MA_V22_OUTCOME" in workflow
    assert "options: [auto, signal, execute, rehearsal]" in workflow
    assert "--all-strategies" in workflow
    assert workflow.count("--mode auto") == 5
    assert '--mode "${{ steps.plan.outputs.action }}"' not in workflow
    assert "if: inputs.mode != 'rehearsal'" in workflow
    assert "python scripts/forward_daily.py --resolve-date" in workflow
    assert "Asia/Shanghai 当前日期，禁止历史回写" not in workflow
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
    assert "  actions: write" in workflow
    assert "gh workflow run pages.yml" not in workflow
    assert "if: success() && steps.plan.outputs.is_trading_day == 'true'" in workflow
    assert "git add --all -- data/forward ':(exclude)data/forward/performance.json' ':(exclude)data/forward/performance_market.json'" in workflow
    assert "git add data/forward/performance.json data/forward/performance_market.json site/performance.json" in workflow
    assert workflow.index("- name: 先提交前向输入与账本") < workflow.index("- name: 刷新每日公开业绩")
    assert workflow.index("- name: 刷新每日公开业绩") < workflow.index("- name: 提交公开业绩")
    assert "git add data/forward data/v5_inputs.json" not in workflow
    assert "python scripts/forward_performance.py --verify-only" in workflow
    assert workflow.index("- name: 核验待发布快照与观察评估") < workflow.index("- name: 提交公开业绩")


def test_pages_validates_new_inputs_and_excludes_old_public_artifacts():
    workflow = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
    assert workflow.index("python scripts/stock_bond_page.py --verify-only") < workflow.index("git commit")
    assert workflow.index("python scripts/stock_bond_page.py --verify-only") < workflow.index("uses: actions/upload-pages-artifact")
    assert 'cron: "30 11 * * 1-5"' in workflow
    assert 'cron: "30 0 * * 2-6"' in workflow
    assert "if: github.event_name != 'push'" in workflow
    assert 'cp site/index.html site/chart.css site/chart.js site/stock-bond.json "$RUNNER_TEMP/stock-bond-site/"' in workflow
    assert "entry-research.json" not in workflow
    assert "scripts/refresh_crossing_assets.py" not in workflow
    assert "scripts/crossing_entry_research.py" not in workflow
    assert "path: site\n" not in workflow
    assert "site/performance.json" not in workflow
    assert "site/audit.json" not in workflow
    assert "git rebase origin/main" in workflow
    assert workflow.index("git rebase origin/main") < workflow.rindex("python scripts/stock_bond_page.py --verify-only")
    quarterly = (ROOT / ".github/workflows/quarterly-update.yml").read_text(encoding="utf-8")
    assert "  schedule:" not in quarterly


def test_v2_is_limited_to_shadow_output() -> None:
    metadata = (ROOT / "data/forward/v1_metadata.json").read_text(encoding="utf-8")
    assert '"v2_mode": "shadow_only"' in metadata
    assert '"v2_output_root": "data/forward/shadow"' in metadata
    assert '"v2_can_write_v1_journal": false' in metadata
