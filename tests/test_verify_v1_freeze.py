import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import verify_v1_freeze


def test_v1冻结文件与当前权威输入一致() -> None:
    result = verify_v1_freeze.verify(check_git=False)
    assert result["version"] == "V1"
    assert result["data_cutoff"] == "2026-08-25"
    assert result["status"] == "通过"


def test_v1冻结校验支持_lf_检出且拒绝正文变化(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = Path(__file__).resolve().parents[1]
    frozen = json.loads((source_root / "data/v1_freeze.json").read_text(encoding="utf-8"))
    for field in ("manifest", "rebalance_dates", "current_result", "strategy_config"):
        relative = Path(frozen["inputs"][field])
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source_root / relative).read_bytes().replace(b"\r\n", b"\n"))

    freeze_path = tmp_path / "data/v1_freeze.json"
    freeze_path.write_text(json.dumps(frozen, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(verify_v1_freeze, "ROOT", tmp_path)
    monkeypatch.setattr(verify_v1_freeze, "FREEZE", freeze_path)

    assert verify_v1_freeze.verify(check_git=False)["status"] == "通过"

    manifest_path = tmp_path / frozen["inputs"]["manifest"]
    manifest_path.write_bytes(manifest_path.read_bytes().replace(b'"schema_version": 1', b'"schema_version": 2', 1))
    with pytest.raises(ValueError, match="V1 文件指纹不匹配"):
        verify_v1_freeze.verify(check_git=False)
