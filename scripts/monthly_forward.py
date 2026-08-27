"""月度 V1 前向观察账本。

账本使用只追加 JSONL 事件流。信号与执行分成两个命令；相同期已有不同内容时
立即拒绝，绝不覆盖。脚本只读取冻结缓存，不连接券商，也不会自动下单。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest
from quarterly_strategy import momentum_filter, screen_dynamic_pool, select_entry_candidates
from universe_manifest import load_manifest, verify_cache_snapshot

FORWARD_DIR = ROOT / "data" / "forward"
METADATA_PATH = FORWARD_DIR / "v1_metadata.json"
JOURNAL_PATH = FORWARD_DIR / "monthly_v1.jsonl"
FORWARD_CACHE_DIR = FORWARD_DIR / "cache"
FORWARD_INPUT_DIR = FORWARD_DIR / "inputs"

V1_COMMIT = "c7d128ff0bc1b4b21c60bc7c6e2894dabf513fae"
V1_START_DATE = "2026-08-25"
V1_FIRST_SIGNAL_DATE = "2026-08-31"
V1_MANIFEST_SHA256 = "24de009d9bb60c857fc89e8f7510b93583b17f9abde50350ea63a6a5830a7409"
V1_DATES_SHA256 = "f62fc22c2f2f972e3b29dea42e2a41202bfa620e702acc3c750e26f8c959ec3e"
V1_DATA_CUTOFF = "2026-08-25"

# 这些值来自 c7d128f 的历史回测口径；PR 上限 999 表示前向观察沿用纯股息率层。
V1_RULES: dict[str, Any] = {
    "initial_capital": 100000.0,
    "frequency": "monthly",
    "entry_yield": 7.5,
    "entry_pr": 999.0,
    "hold_yield": 5.5,
    "loss_hold_yield": 5.5,
    "hold_pr": 999.0,
    "exit_yield": 5.3,
    "exit_pr": 999.0,
    "exit_confirm_quarters": 1,
    "max_holdings": 2,
    "max_sector": 2,
    "max_banks": 2,
    "max_position_pct": 1.0,
    "lot_size": 100,
    "reinvest_dividends": True,
    "reinvest_cash_reserve": 0,
    "rebalance_threshold": 2.0,
    "stop_loss_pct": 0.0,
    "buy_commission_rate": 0.0003,
    "sell_commission_rate": 0.0003,
    "stamp_duty_rate": 0.0005,
    "transfer_fee_rate": 0.00001,
    "min_commission": 5.0,
    "pool_mode": "dynamic",
    "pool_min_consecutive_years": 3,
    "pool_switch_month": 7,
    "momentum_months": 4,
    "momentum_threshold": 0.85,
    "momentum_periods": "",
    "rank_by": "yield",
    "max_yield": 999.0,
    "execution_lag_days": 1,
    "dividend_information_lag_days": 0,
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_dates(path: Path) -> tuple[list[str], str]:
    payload = _read_json(path)
    dates = payload.get("dates") if isinstance(payload, dict) else payload
    if not isinstance(dates, list):
        raise ValueError("月度日期文件缺少 dates")
    normalized = sorted({str(value)[:10] for value in dates})
    actual = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    expected = payload.get("dates_sha256") if isinstance(payload, dict) else None
    if expected and expected != actual:
        raise ValueError("月度日期文件 dates_sha256 校验失败")
    return normalized, actual


def _load_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"前向账本第 {number} 行不是有效 JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"前向账本第 {number} 行不是对象")
        rows.append(row)
    return rows


def _append_once(path: Path, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """按 event_type + period 保证幂等，并拒绝内容漂移。"""
    identity = (event["event_type"], event["period"])
    comparable = dict(event)
    comparable.pop("recorded_at", None)
    for old in _load_journal(path):
        if (old.get("event_type"), old.get("period")) != identity:
            continue
        old_comparable = dict(old)
        old_comparable.pop("recorded_at", None)
        if old_comparable == comparable:
            return old, False
        raise ValueError(f"{identity[1]} 已存在不同的 {identity[0]} 记录，拒绝覆盖")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
    return event, True


def _cache_payload(cache_dir: Path, prefix: str, code: str, default: Any) -> Any:
    path = cache_dir / f"{prefix}_{code}.json"
    return _read_json(path) if path.exists() else default


def _calendar(cache_dir: Path, codes: list[str]) -> list[str]:
    dates = {
        str(day)[:10]
        for code in codes
        for day, price in (_cache_payload(cache_dir, "kl", code, {}) or {}).items()
        if price and len(str(day)) >= 10
    }
    return sorted(dates)


def _input_state(manifest_path: Path, dates_path: Path) -> tuple[dict[str, Any], list[str]]:
    manifest = load_manifest(manifest_path)
    dates, dates_hash = _load_dates(dates_path)
    state = {
        "manifest_records_sha256": manifest["records_sha256"],
        "dates_sha256": dates_hash,
        "data_cutoff": manifest["as_of"],
        "price_format": (manifest.get("source") or {}).get("price_format"),
    }
    return {"manifest": manifest, "input": state}, dates


def _pool(cache_dir: Path, codes: list[str], signal_date: str) -> list[str]:
    summaries = {code: _cache_payload(cache_dir, "dv", code, []) for code in codes}
    details = {code: _cache_payload(cache_dir, "dvd", code, []) for code in codes}
    return screen_dynamic_pool(
        summaries,
        signal_date,
        int(V1_RULES["pool_min_consecutive_years"]),
        dividend_details_by_code=details,
        pool_switch_month=int(V1_RULES["pool_switch_month"]),
    )


def _previous_holdings(journal_path: Path, period: str) -> set[str]:
    executions = [
        row for row in _load_journal(journal_path)
        if row.get("event_type") == "execution" and str(row.get("period") or "") < period
    ]
    if not executions:
        return set()
    latest = max(executions, key=lambda row: str(row.get("period") or ""))
    return {str(row.get("code") or "").zfill(6) for row in latest.get("holdings", [])}


def _require_complete_cache(cache_dir: Path, codes: list[str]) -> None:
    missing = {
        prefix: [code for code in codes if not (cache_dir / f"{prefix}_{code}.json").exists()]
        for prefix in ("kl", "dv", "dvd")
    }
    missing = {prefix: values for prefix, values in missing.items() if values}
    if missing:
        raise ValueError(f"manifest 代码存在缺失缓存，拒绝生成信号: {missing}")


def _historical_input_snapshot(cache_dir: Path, codes: list[str], signal_date: str) -> dict[str, Any]:
    """冻结所有代码截至信号日的价格和已实施分红指纹。"""
    records = []
    for code in codes:
        prices = {
            str(day)[:10]: price
            for day, price in (_cache_payload(cache_dir, "kl", code, {}) or {}).items()
            if str(day)[:10] <= signal_date
        }
        details = [
            row for row in (_cache_payload(cache_dir, "dvd", code, []) or [])
            if str(row.get("ex_date") or "")[:10] <= signal_date
        ]
        records.append({
            "code": code,
            "prices_sha256": _hash(prices),
            "dividend_details_sha256": _hash(details),
        })
    return {"records": records, "records_sha256": _hash(records)}


def _decision_snapshot(
    cache_dir: Path,
    codes: list[str],
    signal_date: str,
    rebalance_dates: list[str],
    held_codes: set[str],
) -> dict[str, Any]:
    """只用 signal_date 及以前数据重建 V1 的完整信号决策。"""
    klines = {
        code: {
            str(day)[:10]: price
            for day, price in (_cache_payload(cache_dir, "kl", code, {}) or {}).items()
            if str(day)[:10] <= signal_date
        }
        for code in codes
    }
    summaries = {code: _cache_payload(cache_dir, "dv", code, []) for code in codes}
    details = {
        code: [
            row for row in (_cache_payload(cache_dir, "dvd", code, []) or [])
            if str(row.get("ex_date") or "")[:10] <= signal_date
        ]
        for code in codes
    }
    pool_codes = screen_dynamic_pool(
        summaries, signal_date, int(V1_RULES["pool_min_consecutive_years"]),
        dividend_details_by_code=details,
        pool_switch_month=int(V1_RULES["pool_switch_month"]),
    )
    active_codes = sorted(set(pool_codes) | held_codes)
    rows = []
    for code in active_codes:
        price, price_date = backtest._find_price_with_date(klines.get(code, {}), signal_date)
        if not price or price <= 0:
            raise ValueError(f"候选或已有持仓在信号日前没有可用收盘价: {code}")
        row = backtest.build_snapshot(
            code, price, summaries.get(code, []), signal_date, details.get(code, [])
        )
        row["signal_date"] = signal_date
        row["signal_price_date"] = price_date
        row["signal_price_age_days"] = (
            datetime.strptime(signal_date, "%Y-%m-%d")
            - datetime.strptime(price_date, "%Y-%m-%d")
        ).days
        rows.append(row)
    applicable_dates = [date for date in rebalance_dates if date <= signal_date]
    price_lookup = lambda code, date: backtest._find_price(klines.get(code, {}), date)
    filtered = momentum_filter(
        rows, held_codes, price_lookup, signal_date, applicable_dates, V1_RULES
    )
    projection_fields = (
        "code", "price", "yield", "real_yield", "pr", "dps", "sustainability",
        "industry", "sector", "bank", "momentum_ratio", "signal_price_date",
        "signal_price_age_days",
    )
    all_projection = [
        {key: row.get(key) for key in projection_fields}
        for row in sorted(rows, key=lambda item: str(item.get("code") or ""))
    ]
    projection = [
        {key: row.get(key) for key in projection_fields}
        for row in sorted(filtered, key=lambda item: str(item.get("code") or ""))
    ]
    eligible = select_entry_candidates(filtered, V1_RULES, excluded=held_codes)
    pool_hash = hashlib.sha256(",".join(pool_codes).encode("utf-8")).hexdigest()
    decision = {
        "pool_codes": pool_codes,
        "pool_codes_sha256": pool_hash,
        "held_codes": sorted(held_codes),
        "all_rows": all_projection,
        "all_rows_sha256": _hash(all_projection),
        "rows": projection,
        "rows_sha256": _hash(projection),
        "eligible_entry_codes": [str(row.get("code") or "").zfill(6) for row in eligible],
    }
    decision["decision_sha256"] = _hash(decision)
    return decision


def build_metadata() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "strategy": "月度高息动量策略 V1 前向观察",
        "mode": "只追加模型账本，不连接券商、不自动下单",
        "v1_commit": V1_COMMIT,
        "forward_start_date": V1_START_DATE,
        "first_signal_date": V1_FIRST_SIGNAL_DATE,
        "first_execution_rule": "首期信号日之后的下一可用缓存交易日收盘；预期 2026-09-01",
        "frozen_backtest_input": {
            "manifest_records_sha256": V1_MANIFEST_SHA256,
            "dates_sha256": V1_DATES_SHA256,
            "data_cutoff": V1_DATA_CUTOFF,
            "price_format": "unadjusted_close",
        },
        "rules": V1_RULES,
        "rules_sha256": _hash(V1_RULES),
        "status": "等待 2026-08-31 收盘后的完整冻结输入，尚未生成信号",
    }


def initialize(metadata_path: Path = METADATA_PATH, journal_path: Path = JOURNAL_PATH) -> None:
    expected = build_metadata()
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    if metadata_path.exists() and _read_json(metadata_path) != expected:
        raise ValueError("V1 元数据已存在但内容不同，拒绝覆盖")
    if not metadata_path.exists():
        metadata_path.write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    if not journal_path.exists():
        journal_path.touch()


def record_signal(
    signal_date: str,
    *,
    manifest_path: Path,
    dates_path: Path,
    cache_dir: Path,
    journal_path: Path = JOURNAL_PATH,
) -> tuple[dict[str, Any], bool]:
    datetime.strptime(signal_date, "%Y-%m-%d")
    if signal_date < V1_FIRST_SIGNAL_DATE:
        raise ValueError(f"前向信号不得早于 {V1_FIRST_SIGNAL_DATE}")
    loaded, dates = _input_state(manifest_path, dates_path)
    manifest = loaded["manifest"]
    dates_payload = _read_json(dates_path)
    if not isinstance(dates_payload, dict) or str(dates_payload.get("as_of") or "")[:10] != signal_date:
        raise ValueError("信号日期文件截止日必须恰好等于信号日")
    dates_manifest_hash = (dates_payload.get("source") or {}).get("manifest_records_sha256")
    if dates_manifest_hash != manifest["records_sha256"]:
        raise ValueError("信号日期文件没有绑定当前 manifest 哈希")
    if signal_date not in dates:
        raise ValueError("信号日不在版本化月度日期文件中，不能自行猜测月末")
    if manifest["as_of"] != signal_date:
        raise ValueError("信号输入截止日必须恰好等于信号日，不能包含未来数据")
    codes = list(manifest["codes"])
    _require_complete_cache(cache_dir, codes)
    verify_cache_snapshot(manifest, cache_dir)
    calendar = _calendar(cache_dir, codes)
    if signal_date not in calendar:
        raise ValueError("缓存中没有信号日真实收盘价")
    held_codes = _previous_holdings(journal_path, signal_date[:7])
    decision = _decision_snapshot(cache_dir, codes, signal_date, dates, held_codes)
    historical_input = _historical_input_snapshot(cache_dir, codes, signal_date)
    pool_codes = decision["pool_codes"]
    missing = [code for code in pool_codes if signal_date not in _cache_payload(cache_dir, "kl", code, {})]
    event = {
        "schema_version": 1,
        "event_type": "signal",
        "period": signal_date[:7],
        "signal_date": signal_date,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "v1_commit": V1_COMMIT,
        "rules_sha256": _hash(V1_RULES),
        "rules": V1_RULES,
        "input": loaded["input"],
        "candidate_pool": {
            "codes": pool_codes,
            "count": len(pool_codes),
            "codes_sha256": decision["pool_codes_sha256"],
        },
        "decision_snapshot": decision,
        "historical_input_snapshot": historical_input,
        "data_gaps": {
            "candidate_codes_missing_signal_close": missing,
            "manifest_codes_missing_cache_files": {},
        },
        "status": "等待下一可用交易日收盘执行",
    }
    event["content_sha256"] = _hash({k: v for k, v in event.items() if k != "recorded_at"})
    return _append_once(journal_path, event)


@contextmanager
def _using_cache(cache_dir: Path) -> Iterator[None]:
    original = backtest.CACHE_DIR
    backtest.CACHE_DIR = cache_dir
    try:
        yield
    finally:
        backtest.CACHE_DIR = original


def record_execution(
    period: str,
    *,
    manifest_path: Path,
    dates_path: Path,
    cache_dir: Path,
    journal_path: Path = JOURNAL_PATH,
) -> tuple[dict[str, Any], bool]:
    loaded, dates = _input_state(manifest_path, dates_path)
    signals = [row for row in _load_journal(journal_path) if row.get("event_type") == "signal"]
    signals.sort(key=lambda row: row["signal_date"])
    current = next((row for row in signals if row.get("period") == period), None)
    if current is None:
        raise ValueError(f"{period} 尚无不可回写信号，不能执行")
    if loaded["manifest"]["as_of"] <= current["signal_date"]:
        raise ValueError("执行快照尚未覆盖信号日后的交易日")
    applicable = [row for row in signals if row["signal_date"] <= current["signal_date"]]
    codes = list(loaded["manifest"]["codes"])
    _require_complete_cache(cache_dir, codes)
    verify_cache_snapshot(loaded["manifest"], cache_dir)
    replay_historical_input = _historical_input_snapshot(cache_dir, codes, current["signal_date"])
    if replay_historical_input != current.get("historical_input_snapshot"):
        raise ValueError("扩展执行快照改变了信号日以前的价格或分红，拒绝执行")
    held_codes = set(current.get("decision_snapshot", {}).get("held_codes", []))
    replay_decision = _decision_snapshot(
        cache_dir, codes, current["signal_date"], dates, held_codes
    )
    if replay_decision != current.get("decision_snapshot"):
        raise ValueError("扩展执行快照改变了已冻结信号决策，拒绝执行")
    calendar = _calendar(cache_dir, codes)
    execution_date = backtest._next_trading_date(calendar, current["signal_date"], 1)
    if execution_date is None:
        raise ValueError("尚无信号日后的下一可用交易日收盘数据")
    if execution_date <= current["signal_date"]:
        raise ValueError("执行日必须严格晚于信号日")

    rules = dict(V1_RULES)
    rules["through_date"] = loaded["manifest"]["as_of"]
    with _using_cache(cache_dir):
        result = backtest.run_backtest(
            rules=rules,
            codes=codes,
            rebalance_dates=[row["signal_date"] for row in applicable],
            dynamic_pool=True,
            verbose=False,
            track_holdings=True,
            return_events=True,
        )
    provenance = next(
        (row for row in result["pool_provenance"] if row["signal_date"] == current["signal_date"]), None
    )
    if not provenance or provenance.get("execution_date") != execution_date:
        raise ValueError("回放执行日与缓存交易日门禁不一致")
    previous_events = []
    previous_executions = [
        row for row in _load_journal(journal_path)
        if row.get("event_type") == "execution" and row.get("period") < period
    ]
    if previous_executions:
        previous_events = previous_executions[-1].get("cumulative_events", [])
    events = result.get("_events", [])
    if events[:len(previous_events)] != previous_events:
        raise ValueError("历史回放事件发生漂移，拒绝追加执行")
    operations = events[len(previous_events):]
    fees = round(sum(float((row.get("fees") or {}).get("total") or 0) for row in operations), 6)
    nav_row = next((row for row in reversed(result["nav_series"]) if row["date"] == execution_date), None)
    if nav_row is None:
        raise ValueError("执行日没有 NAV 记录")
    pool_codes = current["candidate_pool"]["codes"]
    missing_close = [code for code in pool_codes if execution_date not in _cache_payload(cache_dir, "kl", code, {})]
    event = {
        "schema_version": 1,
        "event_type": "execution",
        "period": period,
        "signal_date": current["signal_date"],
        "execution_date": execution_date,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "v1_commit": V1_COMMIT,
        "rules_sha256": _hash(V1_RULES),
        "signal_input": current["input"],
        "execution_input": loaded["input"],
        "candidate_pool": current["candidate_pool"],
        "operations": operations,
        "cumulative_events": events,
        "holdings": result["final_holdings"],
        "cash": nav_row["cash"],
        "fees": fees,
        "nav": nav_row["nav"],
        "data_gaps": {"candidate_codes_missing_execution_close": missing_close},
        "status": "已按缓存真实交易日收盘完成模型执行",
    }
    event["content_sha256"] = _hash({k: v for k, v in event.items() if k != "recorded_at"})
    return _append_once(journal_path, event)


def main() -> int:
    parser = argparse.ArgumentParser(description="月度 V1 只追加前向观察账本")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化冻结元数据与空账本")
    signal = sub.add_parser("signal", help="追加月末收盘信号")
    signal.add_argument("--date", required=True, help="版本化月末信号日 YYYY-MM-DD")
    execute = sub.add_parser("execute", help="追加下一交易日收盘模拟执行")
    execute.add_argument("--period", required=True, help="信号月份 YYYY-MM")
    for command in (signal, execute):
        command.add_argument("--manifest", type=Path, default=FORWARD_INPUT_DIR / "universe_manifest.json")
        command.add_argument("--dates", type=Path, default=FORWARD_INPUT_DIR / "rebalance_dates_monthly.json")
        command.add_argument("--cache-dir", type=Path, default=FORWARD_CACHE_DIR)
        command.add_argument("--journal", type=Path, default=JOURNAL_PATH)
    args = parser.parse_args()
    if args.command == "init":
        initialize()
        print("V1 前向观察已初始化；当前没有伪造信号或成交。")
        return 0
    if args.command == "signal":
        event, appended = record_signal(
            args.date, manifest_path=args.manifest, dates_path=args.dates,
            cache_dir=args.cache_dir, journal_path=args.journal,
        )
    else:
        event, appended = record_execution(
            args.period, manifest_path=args.manifest, dates_path=args.dates,
            cache_dir=args.cache_dir, journal_path=args.journal,
        )
    print(("已追加" if appended else "幂等：已有相同记录") + f" {event['event_type']} {event['period']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
