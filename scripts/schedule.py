"""季度任务幂等门禁：只在季度结束后的首个可用交易日放行。"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "ledgers" / "relaxed.json"
INDEX_URL = "https://qt.gtimg.cn/q=sh000001"


def latest_market_date(timeout: int = 15) -> date:
    response = requests.get(INDEX_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    response.raise_for_status()
    response.encoding = "gbk"
    match = re.search(r'"([^"]+)"', response.text)
    if not match:
        raise RuntimeError("腾讯指数行情没有可解析内容")
    fields = match.group(1).split("~")
    if len(fields) <= 30 or not re.fullmatch(r"\d{14}", fields[30] or ""):
        raise RuntimeError("腾讯指数行情缺少交易时间")
    return datetime.strptime(fields[30][:8], "%Y%m%d").date()


def target_period(run_date: date) -> tuple[str, date]:
    quarter = (run_date.month - 1) // 3 + 1
    if quarter == 1:
        return f"{run_date.year - 1}Q4", date(run_date.year - 1, 12, 31)
    previous = quarter - 1
    month = previous * 3
    day = 31 if month in (3, 12) else 30
    return f"{run_date.year}Q{previous}", date(run_date.year, month, day)


def gate(run_date: date, market_date: date, last_period: str | None, force: bool = False) -> dict[str, str]:
    period, period_end = target_period(run_date)
    in_window = run_date.month in (1, 4, 7, 10) and run_date.day <= 10
    due = (force or in_window) and market_date > period_end and (force or period != last_period)
    if not (force or in_window):
        reason = "不在季度更新窗口"
    elif market_date <= period_end:
        reason = "季度后首个交易日尚未收盘"
    elif period == last_period and not force:
        reason = "本季度已生成"
    else:
        reason = "可以更新"
    return {
        "due": "true" if due else "false",
        "period": period,
        "data_date": market_date.isoformat(),
        "reason": reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--run-date", help="测试用北京时间日期 YYYY-MM-DD")
    parser.add_argument("--market-date", help="测试用最新交易日 YYYY-MM-DD")
    args = parser.parse_args()
    run_date = date.fromisoformat(args.run_date) if args.run_date else datetime.now(ZoneInfo("Asia/Shanghai")).date()
    market_date = date.fromisoformat(args.market_date) if args.market_date else latest_market_date()
    state = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {}
    result = gate(run_date, market_date, state.get("last_processed_period"), args.force)
    for key, value in result.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()

