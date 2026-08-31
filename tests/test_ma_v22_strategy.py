from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ma_v22_strategy import (
    MA_V22_ASSETS,
    build_forward_execution,
    build_forward_signal,
    canonical_sha256,
    compute_target,
    load_inputs,
    run_frozen_backtest,
)
from refresh_ma_v22_inputs import build_inputs


def _rows(count: int, *, last_date: str = "2026-08-31") -> dict[str, list[dict]]:
    result = {}
    for offset, asset in enumerate(MA_V22_ASSETS):
        values = []
        for index in range(count):
            price = 10 + offset + index * (0.02 if asset != "csi" else -0.005)
            values.append({
                "date": f"2026-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}",
                "hfq_open": price, "hfq_close": price,
                "raw_open": price, "raw_close": price + 0.01,
            })
        values[-1]["date"] = last_date
        result[asset] = values
    return result


def test冻结回测精确复现参考指标() -> None:
    result = run_frozen_backtest(ROOT / "data" / "ma_v22_inputs.json")
    assert result["metrics"]["cagr"] == pytest.approx(0.131187120231025, abs=1e-12)
    assert result["metrics"]["sharpe"] == pytest.approx(1.04025101833458, abs=1e-12)
    assert result["metrics"]["max_drawdown"] == pytest.approx(0.135816733430567, abs=1e-12)
    assert result["first_execution_date"] == "2014-03-03"


def test动量和波动窗口不足时拒绝缩短() -> None:
    payload = build_inputs(_rows(126), [], "2026-08-31")
    with pytest.raises(ValueError, match="不足完整"):
        compute_target(payload["inputs"]["prices"], "2026-08-31")


def test负动量风险资产被门控且权重合计为一() -> None:
    payload = build_inputs(_rows(140), [], "2026-08-31")
    target, detail, _ = compute_target(payload["inputs"]["prices"], "2026-08-31")
    assert detail["csi"]["active"] is False
    assert target.get("csi", 0) == 0
    assert math.isclose(sum(target.values()), 1.0)
    assert target["bond"] >= 0


def test前向信号后只用下一交易日开盘并按整手成交(tmp_path: Path) -> None:
    signal_date, execution_date = "2026-08-31", "2026-09-01"
    rows = _rows(140, last_date=signal_date)
    for asset in rows:
        rows[asset].append({
            "date": execution_date,
            "hfq_open": rows[asset][-1]["hfq_open"],
            "hfq_close": rows[asset][-1]["hfq_close"],
            "raw_open": 10.0,
            "raw_close": 11.0,
        })
    signal_payload = build_inputs({asset: values[:-1] for asset, values in rows.items()}, [], signal_date)
    execution_payload = build_inputs(rows, [], execution_date)
    signal_path, execution_path = tmp_path / "signal.json", tmp_path / "execution.json"
    dates_path = tmp_path / "dates.json"
    signal_path.write_text(json.dumps(signal_payload, ensure_ascii=False), encoding="utf-8")
    execution_path.write_text(json.dumps(execution_payload, ensure_ascii=False), encoding="utf-8")
    dates_path.write_text(json.dumps({"dates": [signal_date]}), encoding="utf-8")
    signal = build_forward_signal(signal_date, dates_path, [], signal_path)
    execution = build_forward_execution(signal_date[:7], [signal], execution_path)
    assert execution["execution_date"] == execution_date
    assert execution["execution_timing"] == "open"
    assert all(operation["price"] == 10 for operation in execution["operations"] if operation["side"] == "买入")
    assert all(operation["shares"] % 100 == 0 for operation in execution["operations"] if operation["side"] == "买入")
    assert execution["nav"] > 100000


def test输入哈希被修改后失败关闭(tmp_path: Path) -> None:
    payload = json.loads((ROOT / "data" / "ma_v22_inputs.json").read_text(encoding="utf-8"))
    payload["inputs"]["prices"][0]["assets"]["gold"]["hfq_close"] += 1
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="哈希"):
        load_inputs(path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.__setitem__("assets", {}), "资产清单"),
        (lambda payload: payload["inputs"]["prices"][0]["assets"].pop("bond"), "四项冻结资产"),
        (lambda payload: payload.__setitem__("as_of", "2026-08-29"), "最后日期"),
    ],
)
def test输入结构即使重算哈希仍失败关闭(tmp_path: Path, mutate, message: str) -> None:
    payload = json.loads((ROOT / "data" / "ma_v22_inputs.json").read_text(encoding="utf-8"))
    mutate(payload)
    payload["hashes"]["prices"] = canonical_sha256(payload["inputs"]["prices"])
    content = dict(payload)
    content.pop("content_sha256", None)
    payload["content_sha256"] = canonical_sha256(content)
    path = tmp_path / "invalid-structure.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_inputs(path)
