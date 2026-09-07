"""在隔离目录复算基线并执行项目完整检查。"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent.parent
STRATEGIES = ("v1", "v2", "v3", "v5", "ma_v22")
RunCommand = Callable[[list[str]], None]


def _run_command(command: list[str]) -> None:
    result = subprocess.run(
        command, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        capture_output=True,
    )
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"命令失败（退出码 {result.returncode}）：{' '.join(command)}\n{detail}")


def _first_difference(expected: object, actual: object, path: str = "根") -> str | None:
    if type(expected) is not type(actual):
        return f"{path} 类型不同"
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return f"{path} 字段不同"
        for key in expected:
            difference = _first_difference(expected[key], actual[key], f"{path}.{key}")
            if difference:
                return difference
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path} 长度不同"
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = _first_difference(left, right, f"{path}[{index}]")
            if difference:
                return difference
    elif expected != actual:
        return f"{path} 不同：期望 {expected!r}，实际 {actual!r}"
    return None


def _frozen_rule_arguments() -> list[str]:
    rules = json.loads((ROOT / "data/v1_freeze.json").read_text(encoding="utf-8"))["rules"]
    arguments: list[str] = []
    for key, value in rules.items():
        if key == "reinvest_dividends":
            continue
        if isinstance(value, float):
            rendered = f"{value:.15f}".rstrip("0")
            if rendered.endswith("."):
                rendered += "0"
        else:
            rendered = str(value)
        arguments.extend(("--param", key, rendered))
    return arguments


def run_checks(*, rehearsal: bool = False, run_command: RunCommand = _run_command) -> Path:
    artifact_dir = Path(tempfile.mkdtemp(prefix="dividend-project-check-"))
    reproduced = artifact_dir / "current_best.json"
    print(f"检查产物目录：{artifact_dir}")

    stages = [
        ("冻结输入和指纹", [sys.executable, "scripts/verify_v1_freeze.py"]),
        *[(f"{strategy} 前向合同", [sys.executable, "scripts/monthly_forward.py", "--strategy", strategy, "verify"])
          for strategy in STRATEGIES],
        ("冻结基线复算", [
            sys.executable, "scripts/backtest.py", "--dynamic-pool",
            "--manifest", "data/universe_manifest.json",
            "--rebalance-dates", "data/rebalance_dates_monthly.json",
            *_frozen_rule_arguments(),
            "--json", str(reproduced),
        ]),
    ]
    if rehearsal:
        stages.append(("五策略隔离演练", [
            sys.executable, "scripts/rehearse_forward_cycle.py",
            "--output", str(artifact_dir / "forward_rehearsal.json"),
        ]))
    stages.extend([
        ("公开业绩离线复核", [sys.executable, "scripts/forward_performance.py", "--verify-only"]),
        ("统一入口测试", [sys.executable, "-m", "pytest", "-q", "tests/test_check_project.py"]),
        ("完整测试", [sys.executable, "-m", "pytest", "-q"]),
        ("Python 编译", [sys.executable, "-m", "compileall", "-q", "scripts", "tests"]),
        ("Git 差异格式", ["git", "diff", "--check"]),
    ])

    for name, command in stages:
        print(f"[检查] {name}")
        run_command(command)
        if name == "冻结基线复算":
            expected = json.loads((ROOT / "data/current_best.json").read_text(encoding="utf-8"))
            actual = json.loads(reproduced.read_text(encoding="utf-8"))
            difference = _first_difference(expected, actual)
            if difference:
                raise RuntimeError(f"冻结基线完整结构不一致：{difference}")
    return artifact_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="执行不联网、不覆盖正式数据的完整项目检查")
    parser.add_argument("--rehearsal", action="store_true", help="附加五策略隔离演练（仍不联网）")
    args = parser.parse_args()
    try:
        artifact_dir = run_checks(rehearsal=args.rehearsal)
    except Exception as exc:
        print(f"检查失败：{exc}", file=sys.stderr)
        return 1
    print(f"全部检查通过；隔离产物保存在：{artifact_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
