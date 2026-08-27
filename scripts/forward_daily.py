"""月度 V1 每日调度门禁、输入快照与状态报告。"""
from __future__ import annotations

import argparse
import calendar as month_calendar
import hashlib
import json
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from monthly_forward import (
    FORWARD_CACHE_DIR, FORWARD_INPUT_DIR, JOURNAL_PATH, V1_FIRST_SIGNAL_DATE,
    record_execution, record_signal, _load_journal,
)


def shanghai_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def baostock_trading_days(start: date, end: date) -> list[date]:
    """查询交易日历；登录、接口或空结果异常时失败关闭。"""
    import baostock as bs
    login = bs.login()
    if getattr(login, "error_code", "") != "0":
        raise RuntimeError(f"BaoStock 登录失败: {getattr(login, 'error_msg', '')}")
    try:
        result = bs.query_trade_dates(start_date=start.isoformat(), end_date=end.isoformat())
        if getattr(result, "error_code", "") != "0":
            raise RuntimeError(f"BaoStock 交易日历失败: {getattr(result, 'error_msg', '')}")
        days = []
        while result.next():
            row = dict(zip(result.fields, result.get_row_data()))
            if str(row.get("is_trading_day")) == "1":
                days.append(date.fromisoformat(str(row["calendar_date"])))
        if not days:
            raise RuntimeError("BaoStock 交易日历返回空结果")
        return sorted(set(days))
    finally:
        bs.logout()


def _pending_signal(rows: list[dict]) -> dict | None:
    executed = {row.get("period") for row in rows if row.get("event_type") == "execution"}
    pending = [row for row in rows if row.get("event_type") == "signal" and row.get("period") not in executed]
    if len(pending) > 1:
        raise RuntimeError("前向账本存在多条待执行信号，拒绝自行选择")
    return max(pending, key=lambda row: row["signal_date"]) if pending else None


def decide_action(today: date, rows: list[dict], trading_days) -> dict:
    """返回 signal/execute/noop；任何错过的事件都失败关闭。"""
    pending = _pending_signal(rows)
    if pending:
        signal_day = date.fromisoformat(pending["signal_date"])
        future = trading_days(signal_day + timedelta(days=1), signal_day + timedelta(days=14))
        next_day = min((day for day in future if day > signal_day), default=None)
        if next_day is None:
            raise RuntimeError("无法确认信号后的下一真实交易日")
        if today < next_day:
            return {"action": "noop", "reason": "等待下一真实交易日", "target_date": next_day.isoformat()}
        if today > next_day:
            raise RuntimeError("已错过信号后的下一真实交易日，禁止回写执行")
        return {"action": "execute", "period": pending["period"], "target_date": today.isoformat()}

    first_signal = date.fromisoformat(V1_FIRST_SIGNAL_DATE)
    previous_end = today.replace(day=1) - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    previous_days = trading_days(previous_start, previous_end)
    if previous_days:
        previous_last = max(previous_days)
        previous_period = previous_last.strftime("%Y-%m")
        previous_exists = any(
            row.get("event_type") == "signal" and row.get("period") == previous_period for row in rows
        )
        if previous_last >= first_signal and not previous_exists:
            raise RuntimeError("已错过上月最后真实交易日，禁止补写信号")

    last_calendar_day = month_calendar.monthrange(today.year, today.month)[1]
    month_start = today.replace(day=1)
    month_end = today.replace(day=last_calendar_day)
    month_days = trading_days(month_start, month_end)
    if not month_days:
        raise RuntimeError("无法确认当月真实交易日")
    last_trading_day = max(month_days)
    period = today.strftime("%Y-%m")
    has_signal = any(row.get("event_type") == "signal" and row.get("period") == period for row in rows)
    if has_signal:
        return {"action": "noop", "reason": "本月信号已经存在", "target_date": today.isoformat()}
    if today < last_trading_day:
        return {"action": "noop", "reason": "尚未到当月最后真实交易日",
                "target_date": last_trading_day.isoformat()}
    if today > last_trading_day:
        raise RuntimeError("已错过当月最后真实交易日，禁止补写信号")
    return {"action": "signal", "target_date": today.isoformat(), "period": period}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _portable_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def save_snapshot_and_report(
    as_of: str, action: dict, *, manifest_path: Path, dates_path: Path,
    cache_dir: Path, journal_path: Path, output_dir: Path,
) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dates = json.loads(dates_path.read_text(encoding="utf-8"))
    if manifest.get("as_of") != as_of or dates.get("as_of") != as_of:
        raise RuntimeError("输入截止日与前向事件日期不一致，拒绝保存快照")
    snapshot_dir = output_dir / "snapshots" / as_of
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(manifest_path, snapshot_dir / "universe_manifest.json")
    shutil.copy2(dates_path, snapshot_dir / "rebalance_dates_monthly.json")
    cache_files = sorted(
        path for path in cache_dir.iterdir()
        if path.is_file() and (path.name.startswith(("kl_", "dv_", "dvd_")) or path.name == "price_format.json")
    )
    hashes = [{"path": _portable_path(path),
               "size": path.stat().st_size, "sha256": _file_sha256(path)} for path in cache_files]
    input_snapshot = {
        "schema_version": 1, "as_of": as_of, "action": action,
        "manifest_sha256": _file_sha256(manifest_path),
        "dates_sha256": _file_sha256(dates_path),
        "cache_files": hashes,
        "cache_files_sha256": hashlib.sha256(
            json.dumps(hashes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    (snapshot_dir / "input_hashes.json").write_text(
        json.dumps(input_snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    rows = _load_journal(journal_path)
    status = {
        "schema_version": 1, "as_of": as_of, "latest_action": action,
        "signal_count": sum(row.get("event_type") == "signal" for row in rows),
        "execution_count": sum(row.get("event_type") == "execution" for row in rows),
        "pending_signal": _pending_signal(rows),
        "latest_event": rows[-1] if rows else None,
        "input_snapshot": _portable_path(snapshot_dir / "input_hashes.json"),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    latest = status["latest_event"] or {}
    lines = [
        "# 月度 V1 前向观察状态", "", f"- 截止日：{as_of}",
        f"- 本次门禁：{action.get('action')}", f"- 说明：{action.get('reason', '门禁动作已执行')}",
        f"- 信号记录：{status['signal_count']} 条", f"- 执行记录：{status['execution_count']} 条",
        f"- 最近事件：{latest.get('event_type', '无')} {latest.get('period', '')}", "",
        "本账本仅用于模型前向观察，不连接券商、不自动下单。历史回测不代表未来收益。",
    ]
    (output_dir / "status.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="月度 V1 每日交易日门禁")
    parser.add_argument("--date", help="必须等于 Asia/Shanghai 当前日期")
    parser.add_argument("--mode", choices=("auto", "signal", "execute"), default="auto")
    parser.add_argument("--plan-only", action="store_true", help="只计算门禁，不刷新账本或生成快照")
    parser.add_argument("--plan-json", type=Path, help="把门禁计划写入 JSON，供自动化读取")
    parser.add_argument("--manifest", type=Path, default=FORWARD_INPUT_DIR / "universe_manifest.json")
    parser.add_argument("--dates", type=Path, default=FORWARD_INPUT_DIR / "rebalance_dates_monthly.json")
    parser.add_argument("--cache-dir", type=Path, default=FORWARD_CACHE_DIR)
    parser.add_argument("--journal", type=Path, default=JOURNAL_PATH)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/forward")
    args = parser.parse_args()
    actual_today = shanghai_today()
    today = date.fromisoformat(args.date) if args.date else actual_today
    if today != actual_today:
        raise RuntimeError("显式日期不等于 Asia/Shanghai 当前日，禁止历史回写")
    rows = _load_journal(args.journal)
    planned = decide_action(today, rows, baostock_trading_days)
    if args.mode != "auto" and args.mode != planned["action"]:
        raise RuntimeError(f"显式模式 {args.mode} 不符合交易日门禁 {planned['action']}")
    if args.plan_only:
        if args.plan_json:
            args.plan_json.parent.mkdir(parents=True, exist_ok=True)
            args.plan_json.write_text(
                json.dumps(planned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(json.dumps(planned, ensure_ascii=False))
        return 0
    action = planned
    if action["action"] == "signal":
        record_signal(today.isoformat(), manifest_path=args.manifest, dates_path=args.dates,
                      cache_dir=args.cache_dir, journal_path=args.journal)
    elif action["action"] == "execute":
        record_execution(action["period"], manifest_path=args.manifest, dates_path=args.dates,
                         cache_dir=args.cache_dir, journal_path=args.journal)
    save_snapshot_and_report(today.isoformat(), action, manifest_path=args.manifest,
                             dates_path=args.dates, cache_dir=args.cache_dir,
                             journal_path=args.journal, output_dir=args.output_dir)
    print(json.dumps(action, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
