"""第 25 轮：幸存者偏差审计——退市股票覆盖缺口量化。

目标：检查 A 股已退市股票中是否有满足策略连续分红 3 年门槛的候选，
从而量化当前 210 只现存缓存集合的幸存者偏差风险。

数据来源：
- 退市列表：akshare stock_info_sh_delist / stock_info_sz_delist
- 分红明细：东财 RPT_SHAREBONUS_DET（只读查询，不修改本地缓存）

运行前设置 PYTHONUTF8=1，东财接口串行限流。
"""
from __future__ import annotations
import json, re, time
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def fetch_dividend_history(code: str) -> list[dict]:
    """从东财接口获取逐笔分红记录，仅保留已实施或已完成的分配。"""
    import requests
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPT_SHAREBONUS_DET&columns=ALL"
        f"&filter=(SECURITY_CODE=%22{code}%22)"
        "&pageNumber=1&pageSize=50&sortColumns=REPORT_DATE&sortTypes=-1"
    )
    try:
        resp = requests.get(
            url, timeout=15,
            headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"},
        )
        data = resp.json()
    except Exception:
        return []
    rows = (data.get("result") or {}).get("data") or []
    result = []
    for row in rows:
        progress = str(row.get("ASSIGNMENT_PROGRESS") or row.get("ASSIGN_PROGRESS") or "")
        if "实施" not in progress and "完成" not in progress:
            continue
        ex_date = str(row.get("EX_DIVIDEND_DATE") or "")[:10]
        report_year = str(row.get("REPORT_DATE") or "")[:4]
        try:
            dps_val = float(row.get("BONUS_IT_RATION_BEFORE_TAX") or 0) / 10
        except (TypeError, ValueError):
            dps_val = 0.0
        result.append({"ex_date": ex_date, "report_year": report_year, "dps": dps_val})
    return result


def max_consecutive_years(records: list[dict], as_of: str) -> int:
    """计算截至 as_of 的最长连续正分红年数。"""
    years = set()
    for r in records:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["ex_date"]):
            continue
        if r["ex_date"] > as_of:
            continue
        if r["dps"] > 0 and r["report_year"].isdigit():
            years.add(int(r["report_year"]))
    if not years:
        return 0
    sorted_y = sorted(years, reverse=True)
    best = consecutive = 1
    for i in range(1, len(sorted_y)):
        if sorted_y[i] == sorted_y[i - 1] - 1:
            consecutive += 1
            best = max(best, consecutive)
        else:
            consecutive = 1
    return best


def main():
    manifest = json.loads(
        (ROOT / "data" / "universe_manifest.json").read_text(encoding="utf-8")
    )
    current_codes = set(manifest["codes"])
    as_of = manifest["as_of"]
    manifest_hash = hashlib.sha256(
        manifest.get("records_sha256", "")
    )
    print(f"manifest: {len(current_codes)} codes, as_of={as_of}")
    print(f"manifest records sha256: {manifest_hash}")

    import akshare as ak

    print("获取上交所退市列表...")
    df_sh = ak.stock_info_sh_delist()
    print("获取深交所退市列表...")
    df_sz = ak.stock_info_sz_delist()

    col_sh_code = "公司代码"
    col_sh_name = "公司简称"
    col_sh_date = "暂停上市日期"
    col_sz_code = "证券代码"
    col_sz_name = "证券简称"
    col_sz_date = "终止上市日期"

    delisted: list[dict] = []
    for _, row in df_sh.iterrows():
        code = str(row.get(col_sh_code, "")).zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue
        suspend = str(row.get(col_sh_date, ""))
        delisted.append({
            "code": code,
            "name": str(row.get(col_sh_name, "")),
            "market": "SH",
            "delist_date": suspend[:10] if len(suspend) >= 10 else "",
        })
    for _, row in df_sz.iterrows():
        code = str(row.get(col_sz_code, "")).zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue
        term = str(row.get(col_sz_date, ""))
        delisted.append({
            "code": code,
            "name": str(row.get(col_sz_name, "")),
            "market": "SZ",
            "delist_date": term[:10] if len(term) >= 10 else "",
        })

    seen: set[str] = set()
    unique_delisted: list[dict] = []
    for d in delisted:
        if d["code"] not in current_codes and d["code"] not in seen:
            seen.add(d["code"])
            unique_delisted.append(d)

    print(f"退市总数（排除当前 manifest）: {len(unique_delisted)}")
    recent = [d for d in unique_delisted if d["delist_date"] >= "2015-01-01"]
    print(f"2015 年后退市: {len(recent)}")

    print("查询分红历史（每只间隔 1 秒）...")
    qualified: list[dict] = []
    failed: list[dict] = []
    detail_records: list[dict] = []
    max_consec_counter: Counter = Counter()
    n_zero_records = 0

    for i, stock in enumerate(recent, 1):
        code = stock["code"]
        try:
            records = fetch_dividend_history(code)
        except Exception as e:
            failed.append({"code": code, "name": stock["name"], "error": str(e)})
            time.sleep(0.5)
            continue

        n_records = len(records)
        max_consec = max_consecutive_years(records, as_of)
        max_dps = max((r["dps"] for r in records), default=0.0)

        if n_records == 0:
            n_zero_records += 1
        max_consec_counter[max_consec] += 1

        info = {
            "code": code,
            "name": stock["name"],
            "market": stock["market"],
            "delist_date": stock["delist_date"],
            "dividend_records": n_records,
            "max_consecutive_years": max_consec,
            "max_dps": round(max_dps, 4),
        }
        detail_records.append(info)

        if max_consec >= 3:
            qualified.append(info)

        if i % 20 == 0 or i == len(recent):
            print(f"  进度 {i}/{len(recent)}, 满足 3 年={len(qualified)}")

        time.sleep(0.5)

    n_processed = len(recent) - len(failed)
    n_any_records = n_processed - n_zero_records
    n_one_year = max_consec_counter.get(1, 0)
    n_two_year = max_consec_counter.get(2, 0)

    output = {
        "round": 25,
        "description": "survivorship_bias_audit_delisted_stocks",
        "as_of": as_of,
        "manifest_records_sha256": manifest_hash,
        "current_manifest_count": len(current_codes),
        "delisted_total": len(unique_delisted),
        "delisted_after_2015": len(recent),
        "delisted_with_3yr_consecutive_dividend": len(qualified),
        "summary": {
            "processed": n_processed,
            "failed_to_fetch": len(failed),
            "zero_dividend_records": n_zero_records,
            "has_dividend_records": n_any_records,
            "max_consecutive_0yr": max_consec_counter.get(0, 0),
            "max_consecutive_1yr": n_one_year,
            "max_consecutive_2yr": n_two_year,
            "max_consecutive_3yr_plus": len(qualified),
        },
        "qualified": qualified,
        "failed": failed,
        "all_delisted_detail": detail_records,
        "limitation": (
            "退市列表来自 akshare 的当前快照，可能遗漏部分历史退市股票。"
            "分红数据来自东财 API 的当前返回，部分退市股票的历史分红记录可能不完整。"
            "审计只检查连续分红 3 年的第一道门槛，未做完整回测验证。"
        ),
    }

    out = ROOT / "data" / "round25_survivorship_audit.json"
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果写入 {out}")
    print(f"满足 3 年连续分红: {len(qualified)} / {len(recent)}")
    print(f"无分红记录: {n_zero_records}, 有记录但不足 3 年: {n_any_records - len(qualified)}")
    print(f"API 失败: {len(failed)}")


if __name__ == "__main__":
    main()
