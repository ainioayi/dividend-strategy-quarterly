"""校验 V1 冻结提交、参数、输入指纹和基线结果。"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
FREEZE = ROOT / "data" / "v1_freeze.json"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(*, check_git: bool = True) -> dict[str, Any]:
    frozen = _load(FREEZE)
    inputs = frozen["inputs"]
    paths = {
        "manifest_file_sha256": ROOT / inputs["manifest"],
        "dates_file_sha256": ROOT / inputs["rebalance_dates"],
        "current_result_file_sha256": ROOT / inputs["current_result"],
        "strategy_config_file_sha256": ROOT / inputs["strategy_config"],
    }
    for field, path in paths.items():
        actual = _sha256(path)
        if actual != inputs[field]:
            raise ValueError(f"V1 文件指纹不匹配: {path.relative_to(ROOT)}")

    manifest = _load(paths["manifest_file_sha256"])
    dates = _load(paths["dates_file_sha256"])
    current = _load(paths["current_result_file_sha256"])
    if manifest.get("as_of") != inputs["data_cutoff"]:
        raise ValueError("V1 manifest 截止日不匹配")
    if manifest.get("records_sha256") != inputs["manifest_records_sha256"]:
        raise ValueError("V1 manifest 记录哈希不匹配")
    if dates.get("dates_sha256") != inputs["dates_sha256"]:
        raise ValueError("V1 日期哈希不匹配")
    if len(dates.get("dates") or []) != inputs["date_count"]:
        raise ValueError("V1 日期数量不匹配")
    if current.get("rules") != frozen["rules"]:
        raise ValueError("V1 参数已变化")

    metrics = current.get("metrics") or {}
    for field in ("cagr", "max_drawdown", "sharpe", "ending_nav", "trade_count"):
        if metrics.get(field) != frozen["baseline_metrics"][field]:
            raise ValueError(f"V1 基线指标已变化: {field}")

    if check_git:
        commit = frozen["git"]["commit"]
        subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    return {
        "version": frozen["version"],
        "commit": frozen["git"]["commit"],
        "data_cutoff": inputs["data_cutoff"],
        "manifest_records_sha256": inputs["manifest_records_sha256"],
        "dates_sha256": inputs["dates_sha256"],
        "status": "通过",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-git", action="store_true", help="仅校验文件，不检查 Git 对象")
    args = parser.parse_args()
    print(json.dumps(verify(check_git=not args.skip_git), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
