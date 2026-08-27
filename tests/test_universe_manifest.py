"""候选池 manifest 的离线完整性测试。"""
from pathlib import Path
import json
import pytest

from universe_manifest import build_manifest, load_manifest, verify_cache_snapshot, write_manifest
from build_universe_manifest import records_from_cache


def test_manifest稳定排序并可往返(tmp_path: Path) -> None:
    m = build_manifest([{"code": "000333", "years": 8}, {"code": "600036", "years": 12}], as_of="2026-08-26", top=2, min_years=3)
    p = tmp_path / "u.json"
    write_manifest(p, m)
    assert load_manifest(p)["codes"] == ["000333", "600036"]


def test_manifest篡改会被拒绝(tmp_path: Path) -> None:
    m = build_manifest([{"code": "000333", "years": 8}], as_of="2026-08-26", top=1, min_years=3)
    p = tmp_path / "u.json"
    write_manifest(p, m)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["records"][0]["years"] = 9
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="sha256"):
        load_manifest(p)


def test_manifest记录日期晚于截止日会被拒绝(tmp_path: Path) -> None:
    m = build_manifest(
        [{"code": "000333", "years": 8, "latest_event_date": "2026-08-27"}],
        as_of="2026-08-26", top=1, min_years=3,
    )
    p = tmp_path / "manifest.json"
    write_manifest(p, m)
    with pytest.raises(ValueError, match="晚于 as_of"):
        load_manifest(p)


def test_cache_manifest按行情和实际除权日截断(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "kl_000333.json").write_text(json.dumps({
        "2026-08-25": 10.0,
        "2026-08-26": 11.0,
    }), encoding="utf-8")
    (cache / "dvd_000333.json").write_text(json.dumps([
        {"year": 2024, "ex_date": "2025-06-01", "dps": 1.0},
        {"year": 2025, "ex_date": "2026-08-26", "dps": 2.0},
    ]), encoding="utf-8")

    records = records_from_cache(cache, "2026-08-25")

    assert len(records) == 1
    assert records[0]["data_max_date"] == "2026-08-25"
    assert records[0]["latest_event_date"] == "2025-06-01"
    assert records[0]["years"] == 1
    assert records[0]["total_dps"] == 1.0


def test_manifest会复核缓存哈希(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    prices = {"2024-01-02": 10.0}
    details = [{"year": 2023, "ex_date": "2024-01-03", "dps": 1.0}]
    (cache / "kl_000333.json").write_text(json.dumps(prices), encoding="utf-8")
    (cache / "dvd_000333.json").write_text(json.dumps(details), encoding="utf-8")
    from build_universe_manifest import records_from_cache
    record = records_from_cache(cache, "2024-12-31")[0]
    manifest = build_manifest([record], as_of="2024-12-31", top=0, min_years=0)
    verify_cache_snapshot(manifest, cache)
    prices["2024-01-03"] = 11.0
    (cache / "kl_000333.json").write_text(json.dumps(prices), encoding="utf-8")
    with pytest.raises(ValueError, match="不一致"):
        verify_cache_snapshot(manifest, cache)
