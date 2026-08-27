"""第 19 轮：分红时点字段审计（只读本地文件，不发起批量请求）。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    cache_files = sorted((ROOT / "data" / "backtest_cache").glob("dvd_*.json"))
    records = []
    key_union: set[str] = set()
    for path in cache_files:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(rows, list):
            continue
        records.extend(row for row in rows if isinstance(row, dict))
        for row in rows:
            if isinstance(row, dict):
                key_union.update(row.keys())

    refresh_path = ROOT / "scripts" / "refresh_snapshot.py"
    refresh_text = refresh_path.read_text(encoding="utf-8") if refresh_path.exists() else ""
    result = {
        "round": 19,
        "scope": "本地缓存字段与转换器静态审计",
        "cache_files_checked": len(cache_files),
        "cache_records_checked": len(records),
        "cache_keys": sorted(key_union),
        "fields": {
            "NOTICE_DATE": {"present_in_backtest_cache": "NOTICE_DATE" in key_union},
            "EQUITY_RECORD_DATE": {"present_in_backtest_cache": "EQUITY_RECORD_DATE" in key_union},
            "PLAN_NOTICE_DATE": {
                "present_in_backtest_cache": "PLAN_NOTICE_DATE" in key_union,
                "mapped_in_refresh_snapshot": "plan_notice_date" in refresh_text,
            },
            "ex_date": {"present_in_backtest_cache": "ex_date" in key_union},
        },
        "cross_validation": {
            "api_requests": 0,
            "reason": "本轮只读冻结缓存和转换器，避免批量请求触发东财限流；后续仅允许少量串行样本核对。",
        },
        "future_function_risk": "公告日、登记日或计划公告日若用于信号，必须先按字段日期 <= signal_date 截断；不能把抓取时点当成历史可得时点。",
        "current_boundary": "回测当前只使用已实施逐笔 ex_date；缺少公告日/登记日时，不宣称已测量真实信息延迟。",
        "recommendation": "暂不把缺失字段纳入策略；先在增量缓存中保留原始时点字段，再用少量样本交叉验证字段语义和发布日期边界。",
    }
    output = ROOT / "data" / "round19_dividend_timing_audit.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
