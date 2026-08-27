from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import verify_v1_freeze


def test_v1冻结文件与当前权威输入一致() -> None:
    result = verify_v1_freeze.verify(check_git=False)
    assert result["version"] == "V1"
    assert result["data_cutoff"] == "2026-08-25"
    assert result["status"] == "通过"
