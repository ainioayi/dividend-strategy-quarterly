"""使用沪深交易所公开清单独立核验 BaoStock 历史证券主表。"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import pandas as pd
import requests

from build_historical_universe import ROOT, canonical_sha256, write_json_atomic


DEFAULT_MASTER = ROOT / "data" / "historical" / "security_master.json"
DEFAULT_EXCEPTIONS = ROOT / "data" / "historical" / "security_master_exceptions.json"
DEFAULT_OUTPUT = ROOT / "data" / "historical" / "security_master_verification.json"
SSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.sse.com.cn/assortment/stock/list/share/",
}
ALLOWED_EXCEPTION_CATEGORIES = {
    "security_code_migration",
    "merger_code_migration",
    "merger_listing_date_semantics",
}
ALLOWED_EVIDENCE_HOSTS = {"static.cninfo.com.cn", "query.sse.com.cn"}


def _date_text(value: Any) -> str:
    converted = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(converted) else converted.date().isoformat()


def _is_sh_a(code: str) -> bool:
    return code.startswith(("600", "601", "603", "605", "688", "689"))


def _is_sz_a(code: str) -> bool:
    return code.startswith(("000", "001", "002", "003", "300", "301", "302"))


def _fetch_sse(stock_type: str, company_status: str, *, delisted: bool) -> dict[str, Any]:
    endpoint = (
        "https://query.sse.com.cn/commonQuery.do"
        if delisted else "https://query.sse.com.cn/sseQuery/commonQuery.do"
    )
    response = requests.get(
        endpoint,
        params={
            "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
            "isPagination": "true",
            "STOCK_CODE": "",
            "CSRC_CODE": "",
            "REG_PROVINCE": "",
            "STOCK_TYPE": stock_type,
            "COMPANY_STATUS": company_status,
            "type": "inParams",
            "pageHelp.cacheSize": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.pageSize": "10000",
            "pageHelp.pageNo": "1",
            "pageHelp.endPage": "1",
        },
        headers=SSE_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("result")
    if not isinstance(rows, list):
        raise RuntimeError("上交所股票清单结构异常")
    records = []
    for row in rows:
        code = str(row.get("COMPANY_CODE") if delisted else row.get("A_STOCK_CODE") or "").zfill(6)
        if not _is_sh_a(code):
            continue
        records.append({
            "code": code,
            "name": str(row.get("COMPANY_ABBR") if delisted else row.get("SEC_NAME_CN") or ""),
            "list_date": _date_text(row.get("LIST_DATE")),
            "termination_date": _date_text(row.get("DELIST_DATE")) if delisted else "",
            "exchange": "SH",
        })
    records.sort(key=lambda row: row["code"])
    return {
        "source": endpoint,
        "response_sha256": canonical_sha256(payload),
        "records": records,
    }


def _fetch_szse(catalog_id: str, tab_key: str, *, delisted: bool) -> dict[str, Any]:
    endpoint = "https://www.szse.cn/api/report/ShowReport"
    response = requests.get(
        endpoint,
        params={
            "SHOWTYPE": "xlsx",
            "CATALOGID": catalog_id,
            "TABKEY": tab_key,
            "random": "0.6935816432433362",
        },
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.szse.cn/"},
        timeout=30,
    )
    response.raise_for_status()
    frame = pd.read_excel(io.BytesIO(response.content))
    records = []
    for _, row in frame.iterrows():
        raw_code = row.get("证券代码") if delisted else row.get("A股代码")
        if pd.isna(raw_code):
            continue
        code = str(raw_code).split(".")[0].zfill(6)
        if not _is_sz_a(code):
            continue
        records.append({
            "code": code,
            "name": str(row.get("证券简称") if delisted else row.get("A股简称") or ""),
            "list_date": _date_text(row.get("上市日期") if delisted else row.get("A股上市日期")),
            "termination_date": _date_text(row.get("终止上市日期")) if delisted else "",
            "exchange": "SZ",
        })
    records.sort(key=lambda row: row["code"])
    return {
        "source": endpoint,
        "catalog_id": catalog_id,
        "tab_key": tab_key,
        "response_sha256": hashlib.sha256(response.content).hexdigest(),
        "records": records,
    }


def fetch_official_security_lists() -> dict[str, Any]:
    sse_main = _fetch_sse("1", "2,4,5,7,8", delisted=False)
    sse_star = _fetch_sse("8", "2,4,5,7,8", delisted=False)
    sse_delisted = _fetch_sse("1,8", "3", delisted=True)
    szse_current = _fetch_szse("1110", "tab1", delisted=False)
    szse_delisted = _fetch_szse("1793_ssgs", "tab2", delisted=True)
    current = sorted(
        sse_main["records"] + sse_star["records"] + szse_current["records"],
        key=lambda row: row["code"],
    )
    delisted = sorted(
        sse_delisted["records"] + szse_delisted["records"],
        key=lambda row: row["code"],
    )
    if len({row["code"] for row in current}) != len(current):
        raise RuntimeError("交易所当前股票清单存在重复代码")
    if len({row["code"] for row in delisted}) != len(delisted):
        raise RuntimeError("交易所退市股票清单存在重复代码")
    return {
        "sources": {
            "sse_main": {key: value for key, value in sse_main.items() if key != "records"},
            "sse_star": {key: value for key, value in sse_star.items() if key != "records"},
            "sse_delisted": {key: value for key, value in sse_delisted.items() if key != "records"},
            "szse_current": {key: value for key, value in szse_current.items() if key != "records"},
            "szse_delisted": {key: value for key, value in szse_delisted.items() if key != "records"},
        },
        "current": current,
        "delisted": delisted,
    }


def validate_exception_evidence(
    exceptions: list[dict[str, Any]],
    fetcher: Callable[..., requests.Response] = requests.get,
) -> tuple[list[dict[str, Any]], set[str]]:
    """实际下载官方证据并重算哈希，不能相信例外文件中的自报标志。"""
    codes = [str(row.get("code") or "") for row in exceptions]
    if len(codes) != len(set(codes)):
        raise RuntimeError("证券主表例外存在重复代码")
    validated: list[dict[str, Any]] = []
    validated_codes: set[str] = set()
    for row in exceptions:
        code = str(row.get("code") or "")
        category = row.get("category")
        if not code or category not in ALLOWED_EXCEPTION_CATEGORIES:
            raise RuntimeError(f"{code or '<空代码>'} 例外类型不受支持: {category}")
        evidence = [{
            "url": row.get("evidence_url"),
            "sha256": row.get("evidence_sha256"),
        }] + list(row.get("additional_evidence") or [])
        if not evidence:
            raise RuntimeError(f"{code} 缺少官方证据")
        checked = []
        for item in evidence:
            url = str(item.get("url") or "")
            expected = str(item.get("sha256") or "").lower()
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in ALLOWED_EVIDENCE_HOSTS:
                raise RuntimeError(f"{code} 证据域名不受信任: {url}")
            if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
                raise RuntimeError(f"{code} 证据哈希格式无效")
            response = fetcher(url, headers=SSE_HEADERS, timeout=30)
            response.raise_for_status()
            actual = hashlib.sha256(response.content).hexdigest()
            if actual != expected:
                raise RuntimeError(
                    f"{code} 官方证据哈希不匹配: expected={expected}, actual={actual}"
                )
            checked.append({"url": url, "sha256": actual})
        enriched = dict(row)
        enriched["evidence_integrity_verified"] = True
        enriched["verified_evidence"] = checked
        validated.append(enriched)
        validated_codes.add(code)
    return validated, validated_codes


def _unique_by_code(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    codes = [str(row.get("code") or "") for row in rows]
    if any(not code for code in codes) or len(codes) != len(set(codes)):
        raise RuntimeError(f"{label} 存在空代码或重复代码")
    return {code: row for code, row in zip(codes, rows)}


def build_verification(
    master: dict[str, Any], official: dict[str, Any], exceptions: list[dict[str, Any]],
    validated_exception_codes: set[str] | None = None,
) -> dict[str, Any]:
    master_rows = master.get("records", [])
    official_current_rows = official.get("current", [])
    official_delisted_rows = official.get("delisted", [])
    master_by_code = _unique_by_code(master_rows, "BaoStock 主表")
    official_current = _unique_by_code(official_current_rows, "交易所在市清单")
    official_delisted = _unique_by_code(official_delisted_rows, "交易所退市清单")
    if official_current.keys() & official_delisted.keys():
        raise RuntimeError("交易所在市和退市清单存在代码交集")
    invalid_status = sorted(
        code for code, row in master_by_code.items()
        if row.get("status") not in {"listed", "delisted"}
    )
    if invalid_status:
        raise RuntimeError(f"BaoStock 主表含无效上市状态: {invalid_status[:5]}")
    listed_codes = {code for code, row in master_by_code.items() if row["status"] == "listed"}
    delisted_codes = {code for code, row in master_by_code.items() if row["status"] == "delisted"}
    exception_by_code = _unique_by_code(exceptions, "证券主表例外") if exceptions else {}
    validated_exception_codes = validated_exception_codes or set()

    current_date_differences = []
    for code in sorted(listed_codes & official_current.keys()):
        if master_by_code[code]["list_date"] != official_current[code]["list_date"]:
            current_date_differences.append({
                "code": code,
                "baostock_list_date": master_by_code[code]["list_date"],
                "official_list_date": official_current[code]["list_date"],
            })

    delisted_date_differences = []
    invalid_termination_order = []
    for code in sorted(delisted_codes & official_delisted.keys()):
        last_trading_date = master_by_code[code]["delist_date"]
        termination_date = official_delisted[code]["termination_date"]
        if termination_date < last_trading_date:
            invalid_termination_order.append(code)
        if termination_date != last_trading_date:
            delisted_date_differences.append({
                "code": code,
                "last_trading_date": last_trading_date,
                "official_termination_date": termination_date,
            })

    master_only_delisted = sorted(delisted_codes - official_delisted.keys())
    verified_exceptions = sorted(
        code for code in master_only_delisted
        if code in validated_exception_codes
        and exception_by_code[code].get("evidence_sha256")
        and exception_by_code[code].get("evidence_url")
    )
    unresolved_exceptions = sorted(set(master_only_delisted) - set(verified_exceptions))
    unresolved_list_dates = sorted(
        row["code"] for row in current_date_differences
        if not (
            row["code"] in validated_exception_codes
            and exception_by_code[row["code"]].get("evidence_sha256")
            and exception_by_code[row["code"]].get("evidence_url")
        )
    )
    current_sets_match = listed_codes == official_current.keys()
    official_delisted_is_subset = official_delisted.keys() <= delisted_codes
    independently_verified = (
        current_sets_match
        and official_delisted_is_subset
        and not invalid_termination_order
        and not unresolved_exceptions
        and not unresolved_list_dates
    )
    enriched_records = []
    for code in sorted(master_by_code):
        row = dict(master_by_code[code])
        if code in official_current:
            row["official_list_date"] = official_current[code]["list_date"]
        if code in official_delisted:
            row["official_termination_date"] = official_delisted[code]["termination_date"]
        if code in exception_by_code:
            row["verification_exception"] = exception_by_code[code]
        enriched_records.append(row)
    return {
        "schema_version": 1,
        "source_snapshot_date": master["source_snapshot_date"],
        "provider_complete": True,
        "independently_verified": independently_verified,
        "current_sets_match": current_sets_match,
        "official_delisted_is_subset": official_delisted_is_subset,
        "counts": {
            "master_total": len(master_by_code),
            "master_listed": len(listed_codes),
            "master_delisted": len(delisted_codes),
            "official_current": len(official_current),
            "official_delisted": len(official_delisted),
        },
        "differences": {
            "official_current_not_in_master": sorted(official_current.keys() - listed_codes),
            "master_listed_not_official_current": sorted(listed_codes - official_current.keys()),
            "official_delisted_not_in_master": sorted(official_delisted.keys() - delisted_codes),
            "master_only_delisted": master_only_delisted,
            "unresolved_exceptions": unresolved_exceptions,
            "unresolved_list_dates": unresolved_list_dates,
            "current_list_date_differences": current_date_differences,
            "delisted_date_differences": delisted_date_differences,
            "invalid_termination_order": invalid_termination_order,
        },
        "sources": official.get("sources", {}),
        "records": enriched_records,
        "records_sha256": canonical_sha256(enriched_records),
        "date_semantics": {
            "delist_date": "BaoStock outDate，作为最后可交易边界",
            "official_termination_date": "交易所终止上市生效日，可能晚于最后交易日",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    master = json.loads(args.master.read_text(encoding="utf-8"))
    exceptions = (
        json.loads(args.exceptions.read_text(encoding="utf-8")).get("records", [])
        if args.exceptions.exists() else []
    )
    exceptions, validated_codes = validate_exception_evidence(exceptions)
    payload = build_verification(
        master, fetch_official_security_lists(), exceptions, validated_codes
    )
    write_json_atomic(args.output, payload)
    print(json.dumps({
        "output": str(args.output),
        "independently_verified": payload["independently_verified"],
        "counts": payload["counts"],
        "unresolved_exceptions": payload["differences"]["unresolved_exceptions"],
        "unresolved_list_dates": payload["differences"]["unresolved_list_dates"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
