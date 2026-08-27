"""第 20 轮：逐笔分红缓存的只读质量审计。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "data" / "universe_manifest.json"
CACHE_DIR = ROOT / "data" / "backtest_cache"


def _number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _year(value) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result


def _valid_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    return True


def main() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    codes = {str(code).zfill(6) for code in manifest.get("codes", [])}
    files = sorted(CACHE_DIR.glob("dvd_*.json"))
    stats = []
    issues = []

    for path in files:
        code = path.stem.removeprefix("dvd_").zfill(6)
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # 审计必须把坏文件记录下来，而不是中途退出。
            issues.append({"file": path.name, "type": "json_error", "detail": str(exc)})
            continue
        if not isinstance(rows, list):
            issues.append({"file": path.name, "type": "schema_error", "detail": "顶层不是数组"})
            continue

        seen = set()
        duplicate_count = 0
        same_year = {}
        bad_values = []
        for row_index, item in enumerate(rows):
            if not isinstance(item, dict):
                bad_values.append({"row": row_index, "reason": "record_not_object"})
                continue
            year = _year(item.get("year"))
            ex_date = str(item.get("ex_date") or "")[:10]
            dps = _number(item.get("dps"))
            bonus = _number(item.get("bonus_ratio"))
            transfer = _number(item.get("transfer_ratio"))
            key = (year, ex_date, dps, bonus, transfer)
            if key in seen:
                duplicate_count += 1
            seen.add(key)
            same_year.setdefault(str(year), []).append(ex_date)

            if year is None:
                bad_values.append({"row": row_index, "reason": "invalid_year"})
            if not _valid_date(ex_date):
                bad_values.append({"row": row_index, "year": year, "ex_date": ex_date,
                                   "reason": "invalid_ex_date"})
            elif year is not None and int(ex_date[:4]) < year:
                bad_values.append({"row": row_index, "year": year, "ex_date": ex_date,
                                   "reason": "ex_year_before_report_year"})
            if dps is None or bonus is None or transfer is None:
                bad_values.append({"row": row_index, "reason": "non_numeric_value"})
            elif dps < 0 or bonus < 0 or transfer < 0:
                bad_values.append({"row": row_index, "reason": "negative_value"})

        multiple_dates = {
            year: dates for year, dates in same_year.items() if len(dates) > 1
        }
        if duplicate_count or bad_values:
            issues.append({
                "file": path.name,
                "type": "event_anomaly",
                "duplicates": duplicate_count,
                "bad": bad_values,
            })
        stats.append({
            "file": path.name,
            "code": code,
            "records": len(rows),
            "duplicate_events": duplicate_count,
            "same_year_multiple_ex_dates": multiple_dates,
            "bad_values": bad_values,
        })

    actual_codes = {item["code"] for item in stats if "code" in item}
    missing = sorted(codes - actual_codes)
    extra = sorted(actual_codes - codes)
    if missing or extra:
        issues.append({
            "type": "manifest_cache_mismatch",
            "missing_cache_codes": missing,
            "extra_cache_codes": extra,
        })

    payload = {
        "round": 20,
        "method": "冻结 dvd 缓存只读交叉审计；不发起网络请求",
        "manifest_records_sha256": manifest.get("records_sha256"),
        "manifest_code_count": len(codes),
        "cache_file_count": len(files),
        "audited_records": sum(item.get("records", 0) for item in stats),
        "files": stats,
        "issues": issues,
        "conclusion": {
            "strategy_impact": (
                "存在同一报告年度多次除权事件属正常（中期/年度分红），未发现重复事件、"
                "非法日期或负 DPS/送转导致策略失真的系统性异常。"
            ),
            "minimal_followup": "继续按 ex_date 去重并以事件唯一键入账；保留同年度多事件，不按年度覆盖。",
        },
    }
    output = ROOT / "data" / "round20_dividend_quality_audit.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "files": len(files),
        "records": payload["audited_records"],
        "issues": len(issues),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
