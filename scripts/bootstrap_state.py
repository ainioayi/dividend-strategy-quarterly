"""从已核验的初始审计文件建立两个可持续更新的模型账本。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "data" / "backtest_baseline.json"
LEDGER_DIR = ROOT / "data" / "ledgers"


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _prepare(raw: dict[str, Any], strategy: str) -> dict[str, Any]:
    state = dict(raw)
    events = list(state.get("events") or [])
    buys = list(dict.fromkeys(
        str(event.get("code") or "").zfill(6)
        for event in events
        if event.get("side") == "买入"
    ))
    state.update({
        "strategy": strategy,
        "last_processed_period": "2026Q2",
        "processed_dividends": [],
        "last_actions": [
            {"code": code, "action": "buy", "kind": "initial", "reasons": ["2026-08-24 初始模型建仓"]}
            for code in buys
        ],
        "signal_history": [{
            "period": "初始建仓",
            "as_of": state.get("as_of"),
            "buys": buys,
            "sells": [],
            "holds": [],
            "data_gaps": [],
            "nav": state.get("nav"),
            "cash": state.get("cash"),
        }],
        "model_notice": "研究用模拟账本，不代表用户真实成交，不连接券商。",
    })
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="重建尚未进入季度运行的初始账本")
    args = parser.parse_args()
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    targets = {
        "relaxed.json": (payload["current_relaxed_snapshot_ledger"], "relaxed"),
        "relaxed_cap20.json": (payload["current_relaxed_cap20_snapshot_ledger"], "relaxed_cap20"),
    }
    for filename, (raw, strategy) in targets.items():
        path = LEDGER_DIR / filename
        if path.exists() and not args.force:
            print(f"保留现有账本: {path}")
            continue
        if path.exists() and args.force:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("last_processed_period") != "2026Q2":
                raise SystemExit(f"拒绝覆盖已进入季度运行的账本: {path}")
        _atomic_write(path, _prepare(raw, strategy))
        print(f"建立账本: {path}")


if __name__ == "__main__":
    main()
