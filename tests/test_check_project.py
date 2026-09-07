import json
from pathlib import Path

import pytest

import check_project


def _fake_runner(commands: list[list[str]], reproduced: dict | None = None):
    def run(command: list[str]) -> None:
        commands.append(command)
        if "scripts/backtest.py" in command and reproduced is not None:
            Path(command[command.index("--json") + 1]).write_text(
                json.dumps(reproduced, ensure_ascii=False), encoding="utf-8"
            )
    return run


def test失败后立即停止(monkeypatch, tmp_path):
    monkeypatch.setattr(check_project.tempfile, "mkdtemp", lambda **_: str(tmp_path))
    commands = []

    def fail_first(command):
        commands.append(command)
        raise RuntimeError("预期失败")

    with pytest.raises(RuntimeError, match="预期失败"):
        check_project.run_checks(run_command=fail_first)
    assert len(commands) == 1


def test基线完整结构不一致会失败(monkeypatch, tmp_path):
    monkeypatch.setattr(check_project.tempfile, "mkdtemp", lambda **_: str(tmp_path))
    expected = json.loads((check_project.ROOT / "data/current_best.json").read_text(encoding="utf-8"))
    changed = dict(expected)
    changed["metrics"] = {**expected["metrics"], "cagr": -1}

    with pytest.raises(RuntimeError, match="冻结基线完整结构不一致.*metrics.cagr"):
        check_project.run_checks(run_command=_fake_runner([], changed))


def test复算产物只能写入系统隔离目录(monkeypatch, tmp_path):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.setattr(check_project.tempfile, "mkdtemp", lambda **_: str(isolated))
    expected = json.loads((check_project.ROOT / "data/current_best.json").read_text(encoding="utf-8"))
    commands = []

    artifact_dir = check_project.run_checks(run_command=_fake_runner(commands, expected))

    backtest = next(command for command in commands if "scripts/backtest.py" in command)
    assert artifact_dir == isolated
    assert Path(backtest[backtest.index("--json") + 1]) == isolated / "current_best.json"
    assert not any("--verify-live-calendar" in command for command in commands)
