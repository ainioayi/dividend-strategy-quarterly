import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import monthly_forward as forward
import backtest
from universe_manifest import canonical_hash, records_hash


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
