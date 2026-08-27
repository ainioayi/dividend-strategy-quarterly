"""构建历史点时股票池的可审计状态，不完整时拒绝生成 manifest。

BaoStock 可以补充沪深股票的上市日、退市日和不复权日线，但免费公开源
无法证明退市股票的分红送转记录完整。本脚本因此把标的主数据、价格覆盖和
分红覆盖分开记录；只有导入数据明确通过完整性校验时，才允许输出 manifest。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
CODE_RE = re.compile(r"^\d{6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_COVERAGE = {"complete", "partial", "missing", "unverified", "unsupported"}
REQUIRED_FIELDS = {
    "code", "name", "list_date", "delist_date", "exchange", "source_snapshot_date"
}
DEFAULT_DATA_DIR = ROOT / "data" / "historical"


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_json_atomic(path: Path, payload: Any) -> None:
    """先写同目录临时文件再替换，避免中断留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def read_json_if_exists(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def active_on(record: dict[str, Any], signal_date: str) -> bool:
    """判断股票在历史信号日是否处于上市状态。"""
    validate_date(signal_date, "signal_date")
    if not record["list_date"]:
        return False
    return record["list_date"] <= signal_date and (
        not record["delist_date"] or signal_date <= record["delist_date"]
    )


def validate_date(value: str, field: str, *, allow_empty: bool = False) -> None:
    if allow_empty and value == "":
        return
    if not DATE_RE.fullmatch(value):
        raise ValueError(f"{field} 必须是 YYYY-MM-DD，实际为 {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 不是有效日期: {value!r}") from exc


def validate_record(raw: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(REQUIRED_FIELDS - raw.keys())
    if missing:
        raise ValueError(f"股票记录缺少字段: {', '.join(missing)}")
    record = dict(raw)
    record["code"] = str(record["code"]).strip()
    if not CODE_RE.fullmatch(record["code"]):
        raise ValueError(f"股票代码必须是 6 位数字: {record['code']!r}")
    if not str(record["name"]).strip():
        raise ValueError(f"{record['code']} 缺少股票名称")
    record["name"] = str(record["name"]).strip()
    record["exchange"] = str(record["exchange"]).upper()
    if record["exchange"] not in {"SH", "SZ", "BJ"}:
        raise ValueError(f"{record['code']} 交易所无效: {record['exchange']!r}")
    for field, allow_empty in (("list_date", True), ("delist_date", True),
                               ("source_snapshot_date", False)):
        record[field] = str(record[field] or "")[:10]
        validate_date(record[field], field, allow_empty=allow_empty)
    if record["list_date"] and record["delist_date"] and record["delist_date"] < record["list_date"]:
        raise ValueError(f"{record['code']} 退市日早于上市日")
    for field in ("price_coverage", "dividend_coverage"):
        value = record.get(field)
        if not isinstance(value, dict):
            raise ValueError(f"{record['code']} 缺少 {field} 覆盖说明")
        status = value.get("status")
        if status not in VALID_COVERAGE:
            raise ValueError(f"{record['code']} {field}.status 无效: {status!r}")
        if not str(value.get("source", "")).strip():
            raise ValueError(f"{record['code']} {field}.source 不能为空")
        if status == "complete":
            if not record["list_date"]:
                raise ValueError(f"{record['code']} 上市日未知，不能声明 {field} 完整")
            start = str(value.get("start", ""))
            end = str(value.get("end", ""))
            validate_date(start, f"{field}.start")
            validate_date(end, f"{field}.end")
            required_end = record["delist_date"] or record["source_snapshot_date"]
            if start > record["list_date"] or end < required_end:
                raise ValueError(
                    f"{record['code']} {field} 完整覆盖必须至少从上市日延续到 {required_end}"
                )
    return record


def load_import(path: Path) -> list[dict[str, Any]]:
    """读取采购或用户提供的 JSON/CSV，并执行统一合同校验。"""
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("records") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("JSON 必须是记录数组，或含 records 数组的对象")
    elif path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            for field in ("price_coverage", "dividend_coverage"):
                try:
                    row[field] = json.loads(row[field])
                except (KeyError, json.JSONDecodeError) as exc:
                    raise ValueError(f"CSV 的 {field} 必须是 JSON 对象") from exc
    else:
        raise ValueError("只支持 .json 或 .csv 导入")
    records = [validate_record(row) for row in rows]
    codes = [row["code"] for row in records]
    if len(codes) != len(set(codes)):
        raise ValueError("导入数据包含重复股票代码")
    return sorted(records, key=lambda row: row["code"])


def baostock_code(code: str) -> str:
    if code.startswith(("4", "8", "92")):
        raise ValueError(f"BaoStock 不支持北交所代码 {code}")
    return ("sh." if code.startswith(("5", "6", "9")) else "sz.") + code


def fetch_baostock_basics(codes: Iterable[str], snapshot_date: str) -> list[dict[str, Any]]:
    """串行查询上市/退市主数据；不把未查询价格和分红误标为完整。"""
    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败: {login.error_code} {login.error_msg}")
    records: list[dict[str, Any]] = []
    try:
        for code in codes:
            if not CODE_RE.fullmatch(code):
                raise ValueError(f"股票代码必须是 6 位数字: {code!r}")
            try:
                source_code = baostock_code(code)
            except ValueError:
                records.append(validate_record({
                    "code": code, "name": "未知（BaoStock 不支持）", "list_date": "",
                    "delist_date": "", "exchange": "BJ", "source_snapshot_date": snapshot_date,
                    "source": "baostock-0.9.3", "source_code": "",
                    "security_master_status": "unsupported",
                    "price_coverage": {"status": "unsupported", "source": "baostock",
                                       "reason": "BaoStock 不支持北交所"},
                    "dividend_coverage": {"status": "unsupported", "source": "baostock",
                                          "reason": "BaoStock 不支持北交所"},
                }))
                continue
            result = bs.query_stock_basic(code=source_code)
            if result.error_code != "0":
                raise RuntimeError(
                    f"BaoStock 查询 {code} 失败: {result.error_code} {result.error_msg}"
                )
            rows = []
            while result.next():
                rows.append(dict(zip(result.fields, result.get_row_data())))
            if not rows:
                raise RuntimeError(f"BaoStock 未返回 {code} 的标的主数据")
            row = rows[0]
            records.append(validate_record({
                "code": code,
                "name": row["code_name"],
                "list_date": row["ipoDate"],
                "delist_date": row["outDate"],
                "exchange": source_code[:2].upper(),
                "source_snapshot_date": snapshot_date,
                "source": "baostock-0.9.3/query_stock_basic",
                "source_code": source_code,
                "security_master_status": "listed" if row["status"] == "1" else "delisted",
                "price_coverage": {
                    "status": "unverified", "source": "baostock/query_history_k_data_plus",
                    "price_format": "unadjusted_close", "adjustflag": "3",
                    "reason": "本次只获取标的主数据，尚未逐日校验完整性",
                },
                "dividend_coverage": {
                    "status": "unverified", "source": "baostock/query_dividend_data",
                    "reason": "免费源无法证明退市股票分红送转记录完整",
                },
            }))
    finally:
        bs.logout()
    return records


def fetch_all_security_master(snapshot_date: str) -> dict[str, Any]:
    """获取 BaoStock 全量标的后只保留 type=1 的沪深股票。"""
    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败: {login.error_code} {login.error_msg}")
    try:
        result = bs.query_stock_basic()
        if result.error_code != "0":
            raise RuntimeError(
                f"BaoStock 全量标的查询失败: {result.error_code} {result.error_msg}"
            )
        source_count = 0
        records = []
        while result.next():
            source_count += 1
            row = dict(zip(result.fields, result.get_row_data()))
            if row.get("type") != "1":
                continue
            source_code = row.get("code", "")
            code = source_code.split(".")[-1]
            if not CODE_RE.fullmatch(code) or source_code[:2] not in {"sh", "sz"}:
                continue
            records.append({
                "code": code, "name": row.get("code_name", ""),
                "list_date": row.get("ipoDate", ""), "delist_date": row.get("outDate", ""),
                "exchange": source_code[:2].upper(), "source_code": source_code,
                "status": "listed" if row.get("status") == "1" else "delisted",
                "source_snapshot_date": snapshot_date,
            })
    finally:
        bs.logout()
    records.sort(key=lambda item: item["code"])
    return {
        "schema_version": 1, "source": "baostock-0.9.3/query_stock_basic",
        "source_snapshot_date": snapshot_date, "source_record_count": source_count,
        "filter": "type=1 且交易所为沪深", "provider_complete": True,
        "independently_verified": False, "records_sha256": canonical_sha256(records),
        "record_count": len(records),
        "listed_count": sum(r["status"] == "listed" for r in records),
        "delisted_count": sum(r["status"] == "delisted" for r in records),
        "records": records,
        "limitation": "provider_complete 仅表示 BaoStock 查询成功，不代表官方绝对完整。",
    }


DIVIDEND_FIELD_MAP = {
    "dividPlanAnnounceDate": "plan_announce_date",
    "dividRegistDate": "registration_date", "dividOperateDate": "ex_date",
    "dividPayDate": "payment_date", "dividCashPsBeforeTax": "cash_per_share_before_tax",
    "dividStocksPs": "stock_dividend_per_share",
    "dividReserveToStockPs": "reserve_to_stock_per_share",
}


def _number_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize_dividend_row(row: dict[str, str], report_year: int) -> dict[str, Any]:
    normalized: dict[str, Any] = {"report_year": report_year}
    for source, target in DIVIDEND_FIELD_MAP.items():
        value: Any = row.get(source, "")
        if target.endswith("per_share") or target == "cash_per_share_before_tax":
            value = _number_or_zero(value)
        else:
            value = str(value or "")[:10]
        normalized[target] = value
    return normalized


def max_consecutive_positive_dividend_years(records: list[dict[str, Any]]) -> int:
    years = sorted({int(row["report_year"]) for row in records
                    if _number_or_zero(row.get("cash_per_share_before_tax")) > 0})
    best = current = 0
    previous = None
    for year in years:
        current = current + 1 if previous is not None and year == previous + 1 else 1
        best = max(best, current)
        previous = year
    return best


def build_dividend_artifact(
    by_code: dict[str, dict[str, Any]], snapshot_date: str, start_year: int,
    target_count: int,
) -> dict[str, Any]:
    stocks = sorted(by_code.values(), key=lambda item: item["code"])
    provider_complete = len(stocks) == target_count and all(
        item.get("provider_complete") for item in stocks
    )
    return {
        "schema_version": 1, "source": "baostock-0.9.3/query_dividend_data",
        "source_snapshot_date": snapshot_date,
        "scope": f"outDate>=2015-01-01；report_year>={start_year}",
        "target_count": target_count,
        "completed_stock_count": sum(item.get("provider_complete", False) for item in stocks),
        "failed_stock_count": sum(bool(item.get("errors")) for item in stocks),
        "provider_complete": provider_complete, "independently_verified": False,
        "record_count": sum(len(item.get("records", [])) for item in stocks),
        "stocks_sha256": canonical_sha256(stocks), "stocks": stocks,
        "limitation": "查询成功仅证明 BaoStock 对请求年份正常响应，不证明分红历史无遗漏。",
    }


def collect_delisted_dividends(
    security_master: dict[str, Any], output: Path, snapshot_date: str,
    *, start_year: int = 2012,
) -> dict[str, Any]:
    """逐年采集 2015 年后退市股票分红，按股票断点续跑。"""
    import baostock as bs

    existing = read_json_if_exists(output, {})
    by_code = {item["code"]: item for item in existing.get("stocks", [])}
    targets = [row for row in security_master["records"]
               if row["delist_date"] >= "2015-01-01"]
    snapshot_year = int(snapshot_date[:4])
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败: {login.error_code} {login.error_msg}")
    try:
        for index, stock in enumerate(targets, 1):
            end_year = min(snapshot_year, int(stock["delist_date"][:4]))
            expected_years = list(range(start_year, end_year + 1))
            item = by_code.get(stock["code"], {
                "code": stock["code"], "name": stock["name"],
                "list_date": stock["list_date"], "delist_date": stock["delist_date"],
                "source_code": stock["source_code"], "completed_years": [],
                "errors": [], "records": [],
            })
            completed = set(item.get("completed_years", []))
            errors_by_year = {error["report_year"]: error for error in item.get("errors", [])}
            records_by_year: dict[int, list[dict[str, Any]]] = {}
            for record in item.get("records", []):
                records_by_year.setdefault(record["report_year"], []).append(record)
            for year in expected_years:
                if year in completed:
                    continue
                result = bs.query_dividend_data(
                    code=stock["source_code"], year=str(year), yearType="report"
                )
                if result.error_code != "0":
                    errors_by_year[year] = {
                        "report_year": year, "error_code": result.error_code,
                        "error_message": result.error_msg,
                    }
                    continue
                rows = []
                while result.next():
                    rows.append(normalize_dividend_row(
                        dict(zip(result.fields, result.get_row_data())), year
                    ))
                records_by_year[year] = rows
                completed.add(year)
                errors_by_year.pop(year, None)
            item["completed_years"] = sorted(completed)
            item["errors"] = sorted(errors_by_year.values(), key=lambda row: row["report_year"])
            item["records"] = [row for year in sorted(records_by_year)
                               for row in records_by_year[year]]
            item["expected_years"] = expected_years
            item["provider_complete"] = completed.issuperset(expected_years) and not item["errors"]
            item["independently_verified"] = False
            item["max_consecutive_positive_dividend_years"] = (
                max_consecutive_positive_dividend_years(item["records"])
            )
            by_code[stock["code"]] = item
            write_json_atomic(
                output, build_dividend_artifact(by_code, snapshot_date, start_year, len(targets))
            )
            print(f"分红 {index}/{len(targets)} {stock['code']}，记录 {len(item['records'])}")
    finally:
        bs.logout()
    return build_dividend_artifact(by_code, snapshot_date, start_year, len(targets))


def build_price_artifact(
    by_code: dict[str, dict[str, Any]], snapshot_date: str, target_count: int,
) -> dict[str, Any]:
    stocks = sorted(by_code.values(), key=lambda item: item["code"])
    valid_count = sum(price_record_is_valid(item) for item in stocks)
    provider_complete = len(stocks) == target_count and valid_count == target_count
    return {
        "schema_version": 1, "source": "baostock-0.9.3/query_history_k_data_plus",
        "source_snapshot_date": snapshot_date,
        "scope": "2015-01-01 至退市日；仅连续三年正现金分红的退市股",
        "target_count": target_count,
        "completed_stock_count": valid_count,
        "failed_stock_count": target_count - valid_count,
        "provider_complete": provider_complete, "independently_verified": False,
        "row_count": sum(len(item.get("rows", [])) for item in stocks),
        "stocks_sha256": canonical_sha256(stocks), "stocks": stocks,
        "limitation": "provider_complete 只表示查询成功；未与交易所或商业点时库逐日交叉核验。",
    }


def collect_eligible_delisted_prices(
    dividends: dict[str, Any], output: Path, snapshot_date: str,
) -> dict[str, Any]:
    """仅为曾连续三年正分红的退市股抓取 2015 年后的不复权日线。"""
    import baostock as bs

    targets = [item for item in dividends["stocks"]
               if item.get("max_consecutive_positive_dividend_years", 0) >= 3]
    existing = read_json_if_exists(output, {})
    by_code = {item["code"]: item for item in existing.get("stocks", [])}
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败: {login.error_code} {login.error_msg}")
    fields = "date,code,open,high,low,close,volume,amount,adjustflag,tradestatus"
    try:
        for index, stock in enumerate(targets, 1):
            old = by_code.get(stock["code"])
            if old and price_record_is_valid(old):
                continue
            result = bs.query_history_k_data_plus(
                stock["source_code"], fields, start_date="2015-01-01",
                end_date=stock["delist_date"], frequency="d", adjustflag="3",
            )
            if result.error_code != "0":
                by_code[stock["code"]] = {
                    "code": stock["code"], "name": stock["name"],
                    "delist_date": stock["delist_date"], "provider_complete": False,
                    "independently_verified": False, "error_code": result.error_code,
                    "error_message": result.error_msg, "row_count": 0, "rows": [],
                }
            else:
                source_rows = []
                while result.next():
                    source_rows.append(dict(zip(result.fields, result.get_row_data())))
                rows = [
                    {"date": row["date"], "close": row["close"]}
                    for row in source_rows
                    if row.get("date") and row.get("close") and row.get("tradestatus") == "1"
                ]
                by_code[stock["code"]] = {
                    "code": stock["code"], "name": stock["name"],
                    "delist_date": stock["delist_date"], "price_format": "unadjusted_close",
                    "source_fields": result.fields, "stored_fields": ["date", "close"],
                    "adjustflag": "3", "trade_status_filtered": True,
                    "source_row_count": len(source_rows),
                    "source_rows_sha256": canonical_sha256(source_rows),
                    "empty_tradable_range": not rows,
                    "start_date": rows[0]["date"] if rows else "",
                    "end_date": rows[-1]["date"] if rows else "", "row_count": len(rows),
                    "rows_sha256": canonical_sha256(rows), "provider_complete": True,
                    "independently_verified": False, "rows": rows,
                }
                if not rows:
                    by_code[stock["code"]]["note"] = "查询成功但范围内没有可交易日"
            write_json_atomic(
                output, build_price_artifact(by_code, snapshot_date, len(targets))
            )
            print(
                f"价格 {index}/{len(targets)} {stock['code']}，"
                f"行数 {by_code[stock['code']]['row_count']}"
            )
    finally:
        bs.logout()
    return build_price_artifact(by_code, snapshot_date, len(targets))


def price_record_is_valid(item: dict[str, Any]) -> bool:
    """校验断点价格记录，防止只凭完成标志跳过损坏数据。"""
    rows = item.get("rows")
    if not item.get("provider_complete") or not isinstance(rows, list):
        return False
    if item.get("row_count") != len(rows) or item.get("rows_sha256") != canonical_sha256(rows):
        return False
    if item.get("trade_status_filtered") is not True:
        return False
    if not rows:
        return (
            item.get("start_date") == ""
            and item.get("end_date") == ""
            and item.get("empty_tradable_range") is True
            and isinstance(item.get("source_row_count"), int)
            and item.get("source_row_count") >= 0
        )
    dates = [row.get("date", "") for row in rows]
    return (
        dates == sorted(dates)
        and item.get("start_date") == dates[0]
        and item.get("end_date") == dates[-1]
        and dates[-1] <= item.get("delist_date", "")
        and item.get("adjustflag") == "3"
        and item.get("stored_fields") == ["date", "close"]
        and all(set(row) == {"date", "close"} for row in rows)
    )


def artifact_summary(path: Path) -> dict[str, Any]:
    try:
        portable_path = path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        portable_path = path.as_posix()
    if not path.exists():
        return {"path": portable_path, "exists": False}
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    return {
        "path": portable_path, "exists": True,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "provider_complete": payload.get("provider_complete", False),
        "independently_verified": payload.get("independently_verified", False),
        "record_count": payload.get("record_count", payload.get("row_count")),
        "target_count": payload.get("target_count", payload.get("record_count")),
        "failed_stock_count": payload.get("failed_stock_count", 0),
    }


def build_pipeline_status(data_dir: Path, snapshot_date: str) -> dict[str, Any]:
    paths = {
        "security_master": data_dir / "security_master.json",
        "delisted_dividends": data_dir / "delisted_dividends.json",
        "eligible_delisted_prices": data_dir / "eligible_delisted_prices.json",
    }
    artifacts = {name: artifact_summary(path) for name, path in paths.items()}
    independently_verified = all(
        item.get("independently_verified", False) for item in artifacts.values()
    )
    return {
        "schema_version": 2, "source_snapshot_date": snapshot_date,
        "status": "complete" if independently_verified else "incomplete",
        "manifest_generation_allowed": independently_verified,
        "provider_pipeline_complete": all(
            item.get("provider_complete", False) for item in artifacts.values()
        ),
        "independently_verified": independently_verified, "artifacts": artifacts,
        "limitation": "数据源查询完整与独立验证是两层状态；未独立验证前拒绝生成正式 manifest。",
    }


def build_status(records: list[dict[str, Any]], snapshot_date: str) -> dict[str, Any]:
    records = sorted(records, key=lambda row: row["code"])
    mismatched = [r["code"] for r in records if r["source_snapshot_date"] != snapshot_date]
    if mismatched:
        raise ValueError(f"记录快照日与任务快照日不一致: {', '.join(mismatched)}")
    complete = [r for r in records if r["price_coverage"]["status"] == "complete"
                and r["dividend_coverage"]["status"] == "complete"]
    all_complete = len(complete) == len(records) and bool(records)
    return {
        "schema_version": 1,
        "source_snapshot_date": snapshot_date,
        "status": "complete" if all_complete else "incomplete",
        "manifest_generation_allowed": all_complete,
        "record_count": len(records),
        "complete_record_count": len(complete),
        "required_contract": sorted(REQUIRED_FIELDS),
        "point_in_time_rule": "list_date <= signal_date <= delist_date；在市股票 delist_date 为空",
        "records": records,
        "limitations": [
            "BaoStock 不支持北交所。",
            "标的主数据可返回上市日和退市日，但不等于价格与分红历史完整。",
            "免费公开源无法证明退市股票分红送转记录完整，因此默认拒绝生成回测 manifest。",
        ],
    }


def write_manifest_if_complete(status: dict[str, Any], path: Path) -> None:
    if not status["manifest_generation_allowed"]:
        raise RuntimeError("价格或分红覆盖不完整，拒绝生成历史 manifest")
    payload = {
        "schema_version": 1,
        "as_of": status["source_snapshot_date"],
        "source": "validated_historical_import",
        "records": status["records"],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--import-file", type=Path, help="导入已采购或用户提供的 JSON/CSV")
    source.add_argument("--baostock-codes", nargs="+", help="串行查询指定股票代码的主数据")
    parser.add_argument("--baostock-all-stocks", action="store_true",
                        help="获取 BaoStock 全量沪深股票主数据")
    parser.add_argument("--collect-delisted-dividends", action="store_true",
                        help="断点续采 2015 年后退市股分红")
    parser.add_argument("--collect-eligible-prices", action="store_true",
                        help="断点续采连续三年正分红退市股的不复权日线")
    parser.add_argument("--run-all", action="store_true", help="依次运行全量三阶段")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--snapshot-date", required=True, help="数据快照日 YYYY-MM-DD")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "data" / "historical_universe_status.json")
    parser.add_argument("--manifest-output", type=Path, help="仅完整覆盖时输出 manifest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    validate_date(args.snapshot_date, "snapshot_date")
    pipeline_requested = any((args.baostock_all_stocks, args.collect_delisted_dividends,
                              args.collect_eligible_prices, args.run_all))
    if pipeline_requested:
        master_path = args.data_dir / "security_master.json"
        dividends_path = args.data_dir / "delisted_dividends.json"
        prices_path = args.data_dir / "eligible_delisted_prices.json"
        if args.baostock_all_stocks or args.run_all:
            write_json_atomic(master_path, fetch_all_security_master(args.snapshot_date))
        if args.collect_delisted_dividends or args.run_all:
            master = read_json_if_exists(master_path, None)
            if master is None:
                raise RuntimeError("缺少 security_master.json，请先运行 --baostock-all-stocks")
            collect_delisted_dividends(master, dividends_path, args.snapshot_date)
        if args.collect_eligible_prices or args.run_all:
            dividends = read_json_if_exists(dividends_path, None)
            if dividends is None:
                raise RuntimeError("缺少 delisted_dividends.json，请先采集分红")
            collect_eligible_delisted_prices(dividends, prices_path, args.snapshot_date)
        status = build_pipeline_status(args.data_dir, args.snapshot_date)
        write_json_atomic(args.output, status)
        if args.manifest_output:
            write_manifest_if_complete(status, args.manifest_output)
        print(f"状态写入 {args.output}：{status['status']}")
        return
    if not args.import_file and not args.baostock_codes:
        raise ValueError("必须指定一个数据源或流水线阶段")
    records = (load_import(args.import_file) if args.import_file
               else fetch_baostock_basics(args.baostock_codes, args.snapshot_date))
    status = build_status(records, args.snapshot_date)
    write_json_atomic(args.output, status)
    if args.manifest_output:
        write_manifest_if_complete(status, args.manifest_output)
    print(f"状态写入 {args.output}：{status['status']}，{len(records)} 只")


if __name__ == "__main__":
    main()
