"""按固定截止日顺序补齐腾讯不复权日 K 线缓存。"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from backtest import CACHE_DIR, fetch_kline, _save_cache


def _cached_codes(cache_dir: Path) -> list[str]:
    return sorted({path.stem[4:] for path in cache_dir.glob("dvd_*.json")})


def _fetch_kline_sina(code: str, as_of: str) -> dict[str, float]:
    """通过新浪无复权接口读取完整日线（仅维护脚本的可选依赖）。"""
    import akshare

    if code.startswith("6"):
        market = "sh"
    elif code.startswith(("8", "9")):
        market = "bj"
    else:
        market = "sz"
    frame = akshare.stock_zh_a_daily(
        symbol=market + code,
        start_date="20150101",
        end_date=as_of.replace("-", ""),
        adjust="",
    )
    result = {}
    for row in frame.to_dict("records"):
        day = str(row.get("date") or "")[:10]
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if len(day) == 10 and day <= as_of and close > 0:
            result[day] = close
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="补齐回测 K 线缓存")
    parser.add_argument("--as-of", required=True, help="行情截止日 YYYY-MM-DD")
    parser.add_argument("--cache-dir", default=str(CACHE_DIR))
    parser.add_argument("--retry", type=int, default=2, help="空结果重试次数")
    parser.add_argument("--interval", type=float, default=1.05, help="东财请求间隔秒数")
    parser.add_argument("--source", choices=("sina", "eastmoney"), default="sina",
                        help="不复权行情来源；默认新浪，东财仅作备选")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    if cache_dir.resolve() != CACHE_DIR.resolve():
        raise ValueError("当前刷新器只允许写入项目 data/backtest_cache")
    codes = _cached_codes(cache_dir)
    failed: list[str] = []
    for index, code in enumerate(codes, 1):
        rows = {}
        for attempt in range(max(args.retry, 0) + 1):
            if args.source == "sina":
                try:
                    rows = _fetch_kline_sina(code, args.as_of)
                except Exception:
                    rows = {}
                if not rows:
                    # 新浪对部分北交所新股尚未提供历史序列，再尝试同语义
                    # 的东财不复权接口；失败时仍视为失败，不沿用旧缓存。
                    rows = fetch_kline(code, args.as_of, refresh=True)
                if rows:
                    _save_cache("kl_" + code, rows)
            else:
                rows = fetch_kline(code, args.as_of, refresh=True)
            if rows:
                break
            if attempt < args.retry:
                time.sleep(0.5)
        if not rows:
            failed.append(code)
        if index % 10 == 0 or index == len(codes):
            print(f"进度 {index}/{len(codes)}，有效 {index - len(failed)}，失败 {len(failed)}")
        if index < len(codes):
            time.sleep(max(args.interval, 0.0))

    summary = {
        "as_of": args.as_of,
        "requested": len(codes),
        "succeeded": len(codes) - len(failed),
        "failed": failed,
    }
    if not failed:
        (cache_dir / "price_format.json").write_text(
            json.dumps({
                "format": "unadjusted_close",
                "source": "sina_stock_zh_a_daily" if args.source == "sina" else "eastmoney_push2his",
                "as_of": args.as_of,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
