import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import monthly_forward as forward
import backtest
from refresh_v5_inputs import build_v5_inputs
from universe_manifest import canonical_hash, records_hash
from v5_strategy import V5_ATTACHMENT_SHA256


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _fixture(tmp_path):
    cache = tmp_path / "cache"
    code = "600000"
    prices = {
        "2026-01-30": 9.0,
        "2026-02-27": 9.0,
        "2026-03-31": 9.0,
        "2026-04-30": 9.0,
        "2026-08-31": 10.0,
    }
    details = [
        {"year": 2023, "ex_date": "2024-06-01", "dps": 0.3},
        {"year": 2024, "ex_date": "2025-06-01", "dps": 0.3},
        {"year": 2025, "ex_date": "2026-06-01", "dps": 1.0},
    ]
    summary = [{"year": year, "dps": dps} for year, dps in ((2023, .3), (2024, .3), (2025, 1.0))]
    _write(cache / f"kl_{code}.json", prices)
    _write(cache / f"dvd_{code}.json", details)
    _write(cache / f"dv_{code}.json", summary)
    record = {
        "code": code,
        "data_max_date": "2026-08-31",
        "latest_event_date": "2026-06-01",
        "kline_sha256": canonical_hash(prices),
        "dividend_detail_sha256": canonical_hash(details),
    }
    manifest = {
        "schema_version": 1,
        "as_of": "2026-08-31",
        "rules": {"top": 0, "min_years": 0, "sort": "code"},
        "source": {"path": str(cache), "price_format": "unadjusted_close"},
        "records": [record],
        "codes": [code],
        "records_sha256": records_hash([record]),
    }
    manifest_path = tmp_path / "manifest.json"
    dates_path = tmp_path / "dates.json"
    _write(manifest_path, manifest)
    values = ["2026-01-30", "2026-02-27", "2026-03-31", "2026-04-30", "2026-08-31"]
    dates_hash = hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
    _write(dates_path, {
        "as_of": "2026-08-31",
        "source": {"manifest_records_sha256": manifest["records_sha256"]},
        "dates": values,
        "dates_sha256": dates_hash,
    })
    return manifest_path, dates_path, cache, tmp_path / "journal.jsonl"


def _add_execution_day(manifest_path, cache):
    prices_path = cache / "kl_600000.json"
    prices = json.loads(prices_path.read_text(encoding="utf-8"))
    prices["2026-09-01"] = 10.0
    _write(prices_path, prices)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["as_of"] = "2026-09-01"
    manifest["records"][0]["data_max_date"] = "2026-09-01"
    manifest["records"][0]["kline_sha256"] = canonical_hash(prices)
    manifest["records_sha256"] = records_hash(manifest["records"])
    _write(manifest_path, manifest)


def test_forward_inputs_are_isolated_from_frozen_v1_files():
    assert forward.FORWARD_CACHE_DIR.resolve() != backtest.CACHE_DIR.resolve()
    assert forward.FORWARD_INPUT_DIR.resolve() != (forward.ROOT / "data").resolve()
    assert (forward.FORWARD_INPUT_DIR / "universe_manifest.json").read_bytes() == (
        forward.ROOT / "data" / "universe_manifest.json"
    ).read_bytes()
    assert (forward.FORWARD_INPUT_DIR / "rebalance_dates_monthly.json").read_bytes() == (
        forward.ROOT / "data" / "rebalance_dates_monthly.json"
    ).read_bytes()


def test_v1_forward_contract_locks_rules_capital_and_v2_boundary(tmp_path):
    result = forward.verify_forward_contract()
    assert result["target_allocation_pct"] == 100
    assert result["cash_reserve"] == 0
    assert result["observation_months"] == [6, 12]
    assert result["v2_mode"] == "shadow_only"

    metadata = forward.build_metadata()
    metadata["capital_policy"]["cash_reserve"] = 5000
    changed = tmp_path / "v1_metadata.json"
    _write(changed, metadata)
    with pytest.raises(ValueError, match="冻结合同"):
        forward.verify_forward_contract(metadata_path=changed)

    metadata = forward.build_metadata()
    metadata["observation_policy"]["target_months"] = 5
    _write(changed, metadata)
    with pytest.raises(ValueError, match="冻结合同"):
        forward.verify_forward_contract(metadata_path=changed)


def test_current_workspace_stays_waiting_before_first_signal(tmp_path):
    journal = tmp_path / "journal.jsonl"
    with pytest.raises(ValueError, match="日期文件|早于信号日"):
        forward.record_signal(
            "2026-08-31",
            manifest_path=Path("data/universe_manifest.json"),
            dates_path=Path("data/rebalance_dates_monthly.json"),
            cache_dir=Path("data/backtest_cache"),
            journal_path=journal,
        )
    assert not journal.exists()


def test_signal_and_execution_are_separate_append_only_events(tmp_path):
    manifest, dates, cache, journal = _fixture(tmp_path)
    signal, appended = forward.record_signal(
        "2026-08-31", manifest_path=manifest, dates_path=dates,
        cache_dir=cache, journal_path=journal,
    )
    assert appended is True
    assert signal["candidate_pool"]["codes"] == ["600000"]
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1

    _add_execution_day(manifest, cache)
    executed, appended = forward.record_execution(
        "2026-08", manifest_path=manifest, dates_path=dates,
        cache_dir=cache, journal_path=journal,
    )
    assert appended is True
    assert executed["execution_date"] == "2026-09-01"
    assert executed["execution_date"] > executed["signal_date"]
    assert executed["holdings"][0]["code"] == "600000"
    assert executed["operations"][0]["date"] == "2026-09-01"
    assert executed["fees"] > 0
    assert executed["nav"] > 0
    # 目标投入 100%，只允许留下不足一手加手续费的现金尾差。
    assert 0 <= executed["cash"] < 1005
    assert executed["signal_input"]["data_cutoff"] == "2026-08-31"
    assert executed["execution_input"]["data_cutoff"] == "2026-09-01"
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 2
    repeated, appended = forward.record_execution(
        "2026-08", manifest_path=manifest, dates_path=dates,
        cache_dir=cache, journal_path=journal,
    )
    assert appended is False
    assert repeated["content_sha256"] == executed["content_sha256"]
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 2


def test_repeated_signal_is_idempotent_and_changed_input_is_rejected(tmp_path):
    manifest, dates, cache, journal = _fixture(tmp_path)
    first, appended = forward.record_signal(
        "2026-08-31", manifest_path=manifest, dates_path=dates,
        cache_dir=cache, journal_path=journal,
    )
    assert appended is True
    second, appended = forward.record_signal(
        "2026-08-31", manifest_path=manifest, dates_path=dates,
        cache_dir=cache, journal_path=journal,
    )
    assert appended is False
    assert second["content_sha256"] == first["content_sha256"]

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["records"][0]["note"] = "输入已变化"
    payload["records_sha256"] = records_hash(payload["records"])
    _write(manifest, payload)
    dates_payload = json.loads(dates.read_text(encoding="utf-8"))
    dates_payload["source"]["manifest_records_sha256"] = payload["records_sha256"]
    _write(dates, dates_payload)
    with pytest.raises(ValueError, match="拒绝覆盖"):
        forward.record_signal(
            "2026-08-31", manifest_path=manifest, dates_path=dates,
            cache_dir=cache, journal_path=journal,
        )


def test_execution_requires_next_real_trading_day(tmp_path):
    manifest, dates, cache, journal = _fixture(tmp_path)
    forward.record_signal(
        "2026-08-31", manifest_path=manifest, dates_path=dates,
        cache_dir=cache, journal_path=journal,
    )
    with pytest.raises(ValueError, match="尚未覆盖信号日后"):
        forward.record_execution(
            "2026-08", manifest_path=manifest, dates_path=dates,
            cache_dir=cache, journal_path=journal,
        )


def test_signal_rejects_missing_manifest_cache(tmp_path):
    manifest, dates, cache, journal = _fixture(tmp_path)
    (cache / "dv_600000.json").unlink()
    with pytest.raises(ValueError, match="缺失缓存"):
        forward.record_signal(
            "2026-08-31", manifest_path=manifest, dates_path=dates,
            cache_dir=cache, journal_path=journal,
        )
    assert not journal.exists()


def test_signal_records_stale_candidate_price_without_changing_v1_behavior(tmp_path, monkeypatch):
    manifest, dates, cache, journal = _fixture(tmp_path)
    prices_path = cache / "kl_600000.json"
    prices = json.loads(prices_path.read_text(encoding="utf-8"))
    prices.pop("2026-08-31")
    prices["2026-08-28"] = 9.8
    _write(prices_path, prices)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["records"][0]["data_max_date"] = "2026-08-28"
    payload["records"][0]["kline_sha256"] = canonical_hash(prices)
    payload["records_sha256"] = records_hash(payload["records"])
    _write(manifest, payload)
    dates_payload = json.loads(dates.read_text(encoding="utf-8"))
    dates_payload["source"]["manifest_records_sha256"] = payload["records_sha256"]
    _write(dates, dates_payload)
    monkeypatch.setattr(forward, "_calendar", lambda cache_dir, codes: ["2026-08-31"])
    event, appended = forward.record_signal(
        "2026-08-31", manifest_path=manifest, dates_path=dates,
        cache_dir=cache, journal_path=journal,
    )
    assert appended is True
    assert event["data_gaps"]["candidate_codes_missing_signal_close"] == ["600000"]
    row = event["decision_snapshot"]["all_rows"][0]
    assert row["signal_price_date"] == "2026-08-28"
    assert row["signal_price_age_days"] == 3


def test_execution_rejects_historical_price_drift_after_signal(tmp_path):
    manifest, dates, cache, journal = _fixture(tmp_path)
    signal, _ = forward.record_signal(
        "2026-08-31", manifest_path=manifest, dates_path=dates,
        cache_dir=cache, journal_path=journal,
    )
    assert signal["decision_snapshot"]["rows"][0]["momentum_ratio"] == pytest.approx(10 / 9)
    prices_path = cache / "kl_600000.json"
    prices = json.loads(prices_path.read_text(encoding="utf-8"))
    prices["2026-01-30"] = 8.0
    _write(prices_path, prices)
    _add_execution_day(manifest, cache)
    with pytest.raises(ValueError, match="改变了信号日以前的价格或分红"):
        forward.record_execution(
            "2026-08", manifest_path=manifest, dates_path=dates,
            cache_dir=cache, journal_path=journal,
        )


def test三策略只允许持仓上限不同且影子账本独立():
    profiles = {key: forward.strategy_profile(key) for key in ("v1", "v2", "v3")}
    assert [profiles[key]["max_holdings"] for key in profiles] == [2, 3, 4]
    for key in ("v2", "v3"):
        changed = {
            field for field in forward.V1_RULES
            if profiles[key]["rules"][field] != forward.V1_RULES[field]
        }
        assert changed == {"max_holdings"}
        assert profiles[key]["shadow"] is True
        assert profiles[key]["journal_path"].parent == forward.SHADOW_DIR
        assert profiles[key]["journal_path"] != forward.JOURNAL_PATH
    assert len({profile["journal_path"] for profile in profiles.values()}) == 3


def test三策略冻结合同均为十万元全量投入():
    contracts = {
        key: forward.verify_forward_contract(strategy_id=key)
        for key in ("v1", "v2", "v3")
    }
    assert [contracts[key]["version"] for key in contracts] == ["V1", "V2", "V3"]
    assert all(value["target_allocation_pct"] == 100 for value in contracts.values())
    assert all(value["cash_reserve"] == 0 for value in contracts.values())


def test影子策略核心层禁止写入V1或影子目录之外(tmp_path):
    profile = forward.strategy_profile("v2")
    with pytest.raises(ValueError, match="禁止写入 V1"):
        forward._require_journal_boundary(profile, forward.JOURNAL_PATH)
    with pytest.raises(ValueError, match="只能写入"):
        forward._require_journal_boundary(profile, tmp_path / "v2.jsonl")
    forward._require_journal_boundary(
        profile, tmp_path / "isolated_v2.jsonl", allow_isolated_journal=True
    )


def test_v5使用独立完整合同和独立影子账本():
    profile = forward.strategy_profile("v5")
    assert profile["independent_rules"] is True
    assert profile["max_holdings"] == 6
    assert profile["rules"] != {**forward.V1_RULES, "max_holdings": 6}
    assert profile["journal_path"] == forward.SHADOW_DIR / "monthly_v5.jsonl"
    assert profile["journal_path"] != forward.JOURNAL_PATH
    metadata = forward.build_metadata("v5")
    assert metadata["base_strategy"] is None
    assert "only_rule_change" not in metadata
    assert set(metadata["attachment_sha256"]) == {"report_pdf", "appendix_xlsx"}
    assert metadata["capital_policy"]["target_exposure_range_pct"] == [40, 100]
    assert "target_allocation_pct" not in metadata["capital_policy"]


def test_v5合同校验完整规则附件和输入哈希(tmp_path, monkeypatch):
    input_path = tmp_path / "v5_inputs.json"
    _write(input_path, build_v5_inputs({
        "adjustment_factors": [], "fundamentals": [], "industries": [], "h00922": [],
    }, "2026-08-31", attachment_hashes=V5_ATTACHMENT_SHA256))
    monkeypatch.setitem(forward.FORWARD_STRATEGIES["v5"], "input_path", input_path)
    metadata_path = tmp_path / "v5_metadata.json"
    _write(metadata_path, forward.build_metadata("v5"))
    contract = forward.verify_forward_contract(metadata_path=metadata_path, strategy_id="v5")
    assert contract["target_exposure_range_pct"] == [40, 100]
    assert contract["target_allocation_pct"] is None

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    payload["inputs"]["h00922"].append({"date": "2026-08-31", "close": 100})
    _write(input_path, payload)
    with pytest.raises(ValueError, match="哈希校验失败"):
        forward.verify_forward_contract(metadata_path=metadata_path, strategy_id="v5")


def test_v22使用独立完整合同和开盘成交口径():
    profile = forward.strategy_profile("ma_v22")
    metadata = forward.build_metadata("ma_v22")
    contract = forward.verify_forward_contract(strategy_id="ma_v22")

    assert profile["name"] == "多资产风险预算 V2.2（全球版影子）"
    assert profile["journal_path"] == forward.SHADOW_DIR / "monthly_ma_v22.jsonl"
    assert profile["rules"]["execution_timing"] == "next_trading_day_open"
    assert metadata["base_strategy"] is None
    assert metadata["frozen_backtest_input"]["assets"] == ["510300", "518880", "513100", "511010"]
    assert "开盘模拟成交" in metadata["first_execution_rule"]
    assert contract["target_allocation_pct"] == 100
    assert contract["cash_reserve"] == 0


def test五策略名称和账本路径互不混淆():
    profiles = {key: forward.strategy_profile(key) for key in forward.FORWARD_STRATEGIES}
    assert [profiles[key]["name"] for key in profiles] == [
        "高息动量 V1（2只正式）",
        "高息动量 V2（3只影子）",
        "高息动量 V3（4只影子）",
        "高息动量 V5（附件规则影子）",
        "多资产风险预算 V2.2（全球版影子）",
    ]
    assert len({profile["journal_path"] for profile in profiles.values()}) == 5
