"""月末调仓日期构建与 manifest 绑定测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from build_rebalance_dates import build_dates, dates_hash
from universe_manifest import build_manifest, write_manifest


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "kl_000333.json").write_text(json.dumps({
        "2024-01-29": 10.0,
        "2024-02-26": 10.0,
    }), encoding="utf-8")
    (cache / "kl_600000.json").write_text(json.dumps({
        "2024-01-31": 10.0,
        "2024-02-27": 10.0,
    }), encoding="utf-8")
    manifest = build_manifest(
        [{"code": "000333", "years": 3}, {"code": "600000", "years": 3}],
        as_of="2024-02-27", top=0, min_years=0,
    )
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, manifest)
    return manifest_path, cache


def test_build_dates_uses_all_manifest_codes(tmp_path: Path):
    manifest_path, cache = _fixture(tmp_path)
    result = build_dates(manifest_path, cache, as_of="2024-02-27", start_date="2024-01-01")
    assert result["dates"] == ["2024-01-31", "2024-02-27"]
    assert result["dates_sha256"] == dates_hash(result["dates"])
    assert result["source"]["code_count"] == 2


def test_build_dates_rejects_manifest_as_of_mismatch(tmp_path: Path):
    manifest_path, cache = _fixture(tmp_path)
    with pytest.raises(ValueError, match="manifest.as_of"):
        build_dates(manifest_path, cache, as_of="2024-02-28", start_date="2024-01-01")


def test_build_dates_rejects_missing_manifest_cache(tmp_path: Path):
    manifest_path, cache = _fixture(tmp_path)
    (cache / "kl_600000.json").unlink()
    with pytest.raises(ValueError, match="缓存缺失"):
        build_dates(manifest_path, cache, as_of="2024-02-27", start_date="2024-01-01")
