"""把当前核验快照应用到放宽稳定性模型账本，生成季度买卖信号。"""
from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from optimized_strategy import enrich_rows
from quarterly_strategy import rebalance_quarter, select_entry_candidates


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "strategy.json"
SNAPSHOT = ROOT / "data" / "snapshot_current.json"
LEDGER_DIR = ROOT / "data" / "ledgers"
HISTORY_DIR = ROOT / "data" / "history"


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _tax_rate(acquired: date, ex_date: date) -> float:
    days = max((ex_date - acquired).days, 0)
    if days < 30:
        return 0.20
    if days <= 365:
        return 0.10
    return 0.0


def _apply_dividends(state: dict[str, Any], verified: dict[str, Any], as_of: str) -> dict[str, Any]:
    """按现有回测的简化持有期税口径，把已实施分红记入模型现金。"""
    result = deepcopy(state)
    previous = date.fromisoformat(str(result.get("as_of") or as_of))
    current = date.fromisoformat(as_of)
    source_by_code = {str(row.get("code") or "").zfill(6): row for row in verified.get("rows") or []}
    processed = set(result.get("processed_dividends") or [])
    events = list(result.get("events") or [])
    cash = float(result.get("cash") or 0.0)

    for code, holding in (result.get("holdings") or {}).items():
        source = source_by_code.get(str(code).zfill(6), {})
        records = ((source.get("dividend") or {}).get("implemented_records") or [])
        acquired = date.fromisoformat(str(holding.get("entry_date") or result.get("as_of") or as_of))
        for record in records:
            raw_ex_date = str(record.get("ex_dividend_date") or "")[:10]
            try:
                ex_date = date.fromisoformat(raw_ex_date)
            except ValueError:
                continue
            if not (previous < ex_date <= current):
                continue
            key = "|".join([
                str(code).zfill(6),
                str(record.get("report_date") or ""),
                raw_ex_date,
                str(record.get("cash_div_per_share") or 0),
                str(record.get("bonus_ratio") or 0),
                str(record.get("trans_ratio") or 0),
            ])
            if key in processed:
                continue
            shares_before = int(holding.get("shares") or 0)
            dps = float(record.get("cash_div_per_share") or 0.0)
            gross = shares_before * dps
            tax = gross * _tax_rate(acquired, ex_date)
            net = gross - tax
            split = 1.0 + (float(record.get("bonus_ratio") or 0.0)
                           + float(record.get("trans_ratio") or 0.0)) / 10.0
            shares_after = int(round(shares_before * split))
            holding["shares"] = shares_after
            cash += net
            events.append({
                "date": raw_ex_date,
                "side": "分红",
                "code": str(code).zfill(6),
                "name": holding.get("name"),
                "shares": shares_after,
                "shares_before": shares_before,
                "price": ((source.get("quote") or {}).get("price")),
                "gross": gross,
                "tax": tax,
                "net_cash": net,
                "split_factor": split,
                "reason": "已实施现金分红（沿用回测的简化持有期税口径）",
            })
            processed.add(key)

    result["cash"] = round(cash, 2)
    result["events"] = events
    result["processed_dividends"] = sorted(processed)
    return result


def _rules(config: dict[str, Any], cap20: bool) -> dict[str, Any]:
    rules = dict(config["rules"])
    rules["max_position_pct"] = 0.20 if cap20 else 1.0
    return rules


def _action_summary(actions: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        "buys": [item["code"] for item in actions if item.get("action") == "buy"],
        "sells": [item["code"] for item in actions if item.get("action") == "sell"],
        "holds": [item["code"] for item in actions if item.get("action") == "hold" and item.get("kind") != "data_gap"],
        "data_gaps": [item["code"] for item in actions if item.get("kind") == "data_gap"],
    }


def update_one(
    path: Path,
    verified: dict[str, Any],
    rows: list[dict[str, Any]],
    period: str,
    as_of: str,
    rules: dict[str, Any],
    force: bool,
) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("last_processed_period") == period and not force:
        print(f"{path.name}: {period} 已处理，跳过")
        return state
    state = _apply_dividends(state, verified, as_of)
    next_state = rebalance_quarter(state, rows, as_of, rules)
    actions = list(next_state.pop("actions", []))
    summary = _action_summary(actions)
    next_state["last_actions"] = actions
    next_state["last_processed_period"] = period
    next_state["processed_dividends"] = state.get("processed_dividends") or []
    next_state["candidates"] = [
        str(row.get("code") or "").zfill(6)
        for row in select_entry_candidates(rows, rules)
    ]
    for code, holding in (next_state.get("holdings") or {}).items():
        current = (next_state.get("rows_by_code") or {}).get(code, {})
        if current.get("dps") is not None:
            holding["dps"] = current.get("dps")
    next_state["fees"] = round(sum(
        float((event.get("fees") or {}).get("total") or 0.0)
        for event in next_state.get("events") or []
    ), 2)
    history = list(state.get("signal_history") or [])
    history.append({
        "period": period,
        "as_of": as_of,
        **summary,
        "nav": next_state.get("nav"),
        "cash": next_state.get("cash"),
    })
    next_state["signal_history"] = history
    next_state["model_notice"] = "研究用模拟账本，不代表用户真实成交，不连接券商。"
    _atomic_write(path, next_state)
    _atomic_write(HISTORY_DIR / f"{period}-{path.stem}.json", next_state)
    print(
        f"{path.name}: 买入 {summary['buys'] or '-'}；卖出 {summary['sells'] or '-'}；"
        f"净资产 {next_state.get('nav')}"
    )
    return next_state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--snapshot", default=str(SNAPSHOT))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"\d{4}Q[1-4]", args.period):
        raise SystemExit("period 必须是 YYYYQn")
    date.fromisoformat(args.as_of)

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    verified = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    if verified.get("as_of") != args.as_of:
        raise SystemExit("快照日期与 --as-of 不一致")
    source_rows = list(verified.get("candidate_rows") or [])
    relaxed_rows = enrich_rows(
        verified,
        source_rows,
        snapshots={},
        rules={"min_persistence": 0},
    )
    if not relaxed_rows:
        raise SystemExit("当前快照没有可评估股票")

    update_one(
        LEDGER_DIR / "relaxed.json", verified, relaxed_rows,
        args.period, args.as_of, _rules(config, False), args.force,
    )
    update_one(
        LEDGER_DIR / "relaxed_cap20.json", verified, relaxed_rows,
        args.period, args.as_of, _rules(config, True), args.force,
    )
    _atomic_write(ROOT / "data" / "snapshots" / f"{args.period}.json", verified)


if __name__ == "__main__":
    main()

