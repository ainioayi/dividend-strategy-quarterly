"""记录各策略账本动作的独立健康状态，供公开页面揭示影子失败。"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from monthly_forward import FORWARD_STRATEGIES, strategy_profile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "forward" / "shadow" / "health.json"


def build_health(as_of: str, action: str, outcomes: dict[str, str]) -> dict:
    strategies = {}
    for strategy_id in FORWARD_STRATEGIES:
        outcome = outcomes[strategy_id]
        strategies[strategy_id] = {
            "name": strategy_profile(strategy_id)["short_name"],
            "status": "正常" if outcome == "success" else "失败，未冒充更新成功",
            "outcome": outcome,
            "as_of": as_of,
            "action": action,
        }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "as_of": as_of,
        "action": action,
        "strategies": strategies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="写入五策略前向动作健康状态")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--action", choices=("signal", "execute"), required=True)
    for strategy_id in FORWARD_STRATEGIES:
        parser.add_argument(f"--{strategy_id}", choices=("success", "failure"), required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = build_health(
        args.as_of,
        args.action,
        {key: getattr(args, key) for key in FORWARD_STRATEGIES},
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(args.output)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
