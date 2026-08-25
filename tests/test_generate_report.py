"""动态报告生成器的离线回归测试。"""
from __future__ import annotations

import generate_report


def _ledger() -> dict:
    return {
        "as_of": "2026-08-24",
        "last_processed_period": "2026Q2",
        "cash": 1000.0,
        "holdings": {
            "000001": {
                "code": "000001",
                "name": "示例公司",
                "shares": 100,
                "entry_price": 10.0,
                "entry_yield": 6.0,
                "entry_pr": 0.5,
                "dps": 0.6,
                "sector": "示例行业",
            }
        },
        "last_actions": [
            {
                "code": "000001",
                "action": "hold",
                "kind": "normal",
                "reasons": [],
            }
        ],
        "signal_history": [],
    }


def test_build_page只插入一次自动信号并改为独立仓库链接() -> None:
    template = generate_report.TEMPLATE.read_text(encoding="utf-8")
    ledger = _ledger()
    section = generate_report.build_signal_section(ledger, ledger)

    page = generate_report.build_page(template, section, ledger)

    assert page.count(generate_report.AUTO_START) == 1
    assert page.count(generate_report.AUTO_END) == 1
    assert "本季度自动调仓信号" in page
    assert "https://github.com/ainioayi/dividend-strategy-quarterly" in page
    assert 'href="audit.json"' in page


def test模型净资产按当前核验价计算() -> None:
    ledger = _ledger()
    ledger["rows_by_code"] = {"000001": {"price": 12.0, "yield": 5.0, "pr": 0.7}}

    summary = generate_report._ledger_summary(ledger)

    assert summary["market_value"] == 1200.0
    assert summary["nav"] == 2200.0
    assert summary["holding_count"] == 1


def test审计文件保留十年基线并附加两个模型账本() -> None:
    ledger = _ledger()
    config = {
        "automatic_update": {"timezone": "Asia/Shanghai"},
        "candidate_source": "https://example.test/latest.json",
        "upstream_repository": "owner/repo",
        "upstream_commit": "abc123",
    }

    audit = generate_report.build_audit(
        {"report_date": "2026-08-25", "backtest": {"strategy": {}}},
        ledger,
        ledger,
        config,
    )

    assert audit["report_date"] == "2026-08-25"
    assert audit["site_schema_version"] == "2.0"
    assert set(audit["model_ledgers"]) == {"relaxed", "relaxed_cap20"}
    assert audit["latest_model_period"] == "2026Q2"
