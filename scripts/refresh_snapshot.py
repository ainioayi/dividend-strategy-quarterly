"""串行核验季度候选与现有持仓，生成可审计的当前快照。"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from statistics import median
from typing import Any

import requests


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "strategy.json"
LEDGER_DIR = ROOT / "data" / "ledgers"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _implemented(progress: Any) -> bool:
    value = str(progress or "")
    return ("实施" in value and "未实施" not in value) or "完成" in value


def _dividend_summary(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    by_year: dict[str, float] = {}
    annual_years: set[str] = set()
    records: list[dict[str, Any]] = []
    for row in rows or []:
        if not _implemented(row.get("ASSIGN_PROGRESS")):
            continue
        report = str(row.get("REPORT_DATE") or "")[:10]
        dps10 = _number(row.get("PRETAX_BONUS_RMB"))
        if len(report) < 4 or dps10 is None or dps10 <= 0:
            continue
        year = report[:4]
        by_year[year] = by_year.get(year, 0.0) + dps10 / 10.0
        if len(report) == 10 and report[5:] == "12-31":
            annual_years.add(year)
        records.append({
            "report_date": report,
            "ex_dividend_date": str(row.get("EX_DIVIDEND_DATE") or "")[:10],
            "plan_notice_date": str(row.get("PLAN_NOTICE_DATE") or "")[:10],
            "cash_div_per_share": dps10 / 10.0,
            "bonus_ratio": _number(row.get("BONUS_RATIO")) or 0.0,
            "trans_ratio": _number(row.get("TRANSFER_RATIO")) or 0.0,
            "progress": str(row.get("ASSIGN_PROGRESS") or ""),
        })
    complete = max(annual_years) if annual_years else None
    return {
        "latest_complete_year": complete,
        "dps_per_share": by_year.get(complete) if complete else None,
        "years": by_year,
        "implemented_records": sorted(records, key=lambda item: item["ex_dividend_date"]),
    }


def _latest_annual(rows: list[dict[str, Any]] | None, target_year: str | None = None) -> dict[str, Any] | None:
    annual = [
        row for row in (rows or [])
        if len(str(row.get("REPORT_DATE") or "")[:10]) == 10
        and str(row.get("REPORT_DATE") or "")[:10][5:] == "12-31"
    ]
    annual.sort(key=lambda row: str(row.get("REPORT_DATE") or ""), reverse=True)
    if target_year:
        matched = next((row for row in annual if str(row.get("REPORT_DATE") or "")[:4] == target_year), None)
        if matched:
            return matched
    return annual[0] if annual else None


def _roe_median_5y(rows: list[dict[str, Any]] | None) -> float | None:
    annual = [
        row for row in (rows or [])
        if str(row.get("REPORT_DATE") or "")[:10].endswith("12-31")
    ]
    annual.sort(key=lambda row: str(row.get("REPORT_DATE") or ""), reverse=True)
    values = [value for value in (_number(row.get("ROEJQ")) for row in annual[:5]) if value is not None and value > 0]
    return median(values) if values else None


def _load_candidates(path: str | None, urls: list[str]) -> tuple[list[dict[str, Any]], str]:
    if path:
        source = Path(path)
        return json.loads(source.read_text(encoding="utf-8")), str(source)
    errors = []
    for url in urls:
        try:
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list) and payload:
                return payload, url
            errors.append(f"{url}: 返回空列表")
        except Exception as exc:  # noqa: BLE001 - 需要把全部候选源错误写入审计
            errors.append(f"{url}: {exc}")
    raise RuntimeError("候选快照全部不可用：" + "；".join(errors))


def _load_holdings() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(LEDGER_DIR.glob("*.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        for code, holding in (state.get("holdings") or {}).items():
            result.setdefault(str(code).zfill(6), holding)
    return result


def _load_upstream(upstream_root: Path):
    if not (upstream_root / "src").is_dir():
        raise RuntimeError(f"上游源码目录无效: {upstream_root}")
    sys.path.insert(0, str(upstream_root))
    from src.eastmoney_fetcher import fetch_dividend_rows, fetch_financial_rows  # type: ignore
    from src.pr_calculator import (  # type: ignore
        classify_industry,
        classify_valuation,
        compute_basic_pr,
        compute_corrected_pr,
        compute_n_factor,
    )
    from src.sustainability import assess_with_auto_fetch  # type: ignore
    from src.tencent_quote import fetch_tencent_quote_batch  # type: ignore

    return {
        "fetch_dividend_rows": fetch_dividend_rows,
        "fetch_financial_rows": fetch_financial_rows,
        "fetch_quotes": fetch_tencent_quote_batch,
        "classify_industry": classify_industry,
        "classify_valuation": classify_valuation,
        "compute_basic_pr": compute_basic_pr,
        "compute_corrected_pr": compute_corrected_pr,
        "compute_n_factor": compute_n_factor,
        "assess": assess_with_auto_fetch,
    }


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def refresh(
    upstream_root: Path,
    as_of: str,
    candidate_file: str | None,
    out: Path,
    delay: float,
) -> dict[str, Any]:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    candidate_rows, candidate_source = _load_candidates(
        candidate_file,
        [config["candidate_source"], config["candidate_source_fallback"]],
    )
    page_by_code = {str(row.get("代码") or "").zfill(6): row for row in candidate_rows}
    holdings = _load_holdings()
    codes = sorted(set(page_by_code) | set(holdings))
    api = _load_upstream(upstream_root)
    quotes = api["fetch_quotes"](codes)
    output_rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    complete_candidates = 0

    for index, code in enumerate(codes, 1):
        page = page_by_code.get(code, {})
        holding = holdings.get(code, {})
        quote = quotes.get(code)
        dividend_rows = None
        financial_rows = None
        try:
            dividend_rows = api["fetch_dividend_rows"](code)
        except Exception as exc:  # noqa: BLE001
            errors.append({"code": code, "stage": "dividend", "error": str(exc)})
        time.sleep(delay)
        try:
            financial_rows = api["fetch_financial_rows"](code)
        except Exception as exc:  # noqa: BLE001
            errors.append({"code": code, "stage": "financial", "error": str(exc)})
        time.sleep(delay)

        dividend = _dividend_summary(dividend_rows)
        financial = _latest_annual(financial_rows, dividend.get("latest_complete_year"))
        price = _number(getattr(quote, "price", None))
        total_shares = _number(getattr(quote, "total_shares", None))
        pe_ttm = _number(getattr(quote, "pe_ttm", None))
        pb = _number(getattr(quote, "pb", None))
        dps = _number(dividend.get("dps_per_share"))
        net_profit = _number((financial or {}).get("PARENTNETPROFIT"))
        operating_cf = _number((financial or {}).get("NETCASH_OPERATE_PK"))
        roe_latest = _number((financial or {}).get("ROEJQ"))
        industry = str(page.get("行业") or holding.get("sector") or "未知行业")
        dividend_cash = dps * total_shares if dps and total_shares else None
        payout_decimal = dividend_cash / net_profit if dividend_cash and net_profit and net_profit > 0 else None
        is_cyclical = api["classify_industry"](industry)[0]
        roe_for_pr = _roe_median_5y(financial_rows) if is_cyclical else roe_latest
        n_factor = api["compute_n_factor"](payout_decimal)
        pr = (
            api["compute_corrected_pr"](pe_ttm, roe_for_pr, n_factor)
            if n_factor is not None
            else api["compute_basic_pr"](pe_ttm, roe_for_pr)
        )
        real_yield = dps / price * 100.0 if dps and price else _number(page.get("真实股息率%"))
        verdict = str(page.get("可持续性") or "")
        if not verdict:
            try:
                assessed = api["assess"](
                    stock_code=code,
                    total_shares=total_shares or 0.0,
                    dividend_total=dividend_cash,
                    dividend_yield_before_tax=real_yield,
                    latest_dividend_year=dividend.get("latest_complete_year"),
                    industry=industry,
                    dividend_rows=dividend_rows,
                    financial_rows=financial_rows,
                )
                verdict = str(getattr(assessed, "verdict", "未评估"))
            except Exception as exc:  # noqa: BLE001
                verdict = "未评估"
                errors.append({"code": code, "stage": "sustainability", "error": str(exc)})

        valid = bool(quote and dps and financial and price and total_shares and real_yield and pr)
        if code in page_by_code and valid:
            complete_candidates += 1
        if not quote:
            errors.append({"code": code, "stage": "quote", "error": "腾讯报价缺失"})
        if dividend_rows is None:
            errors.append({"code": code, "stage": "dividend", "error": "东财分红请求失败"})
        if financial_rows is None:
            errors.append({"code": code, "stage": "financial", "error": "东财富务请求失败"})

        output_rows.append({
            "code": code,
            "name": page.get("名称") or holding.get("name") or getattr(quote, "name", None) or code,
            "industry": industry,
            "is_candidate": code in page_by_code,
            "page_real_yield": real_yield,
            "page_pr": pr,
            "page_sustainability": verdict or "未评估",
            "page_zone": api["classify_valuation"](pr),
            "page_updated": as_of,
            "source_page_real_yield": _number(page.get("真实股息率%")),
            "source_page_pr": _number(page.get("市赚率PR")),
            "quote": ({
                "price": price,
                "pe_ttm": pe_ttm,
                "pb": pb,
                "total_shares": total_shares,
                "market_cap_yi": price * total_shares / 1e8 if price and total_shares else None,
            } if quote else None),
            "dividend": dividend,
            "financial": ({
                "report_date": str(financial.get("REPORT_DATE") or "")[:10],
                "roe": roe_latest,
                "roe_5y_median": _roe_median_5y(financial_rows),
                "net_profit": net_profit,
                "operating_cf": operating_cf,
                "capital_adequacy": _number(financial.get("NEWCAPITALADER")),
            } if financial else None),
        })
        print(f"[{index}/{len(codes)}] {code} {output_rows[-1]['name']} ({'完整' if valid else '缺项'})")

    candidate_count = len(page_by_code)
    success_ratio = complete_candidates / candidate_count if candidate_count else 0.0
    missing_holding_quotes = sorted(code for code in holdings if code not in quotes)
    if candidate_count < 10:
        raise RuntimeError(f"候选池异常偏小: {candidate_count}")
    if success_ratio < 0.8:
        raise RuntimeError(f"候选完整率只有 {success_ratio:.1%}，停止发布")
    if missing_holding_quotes:
        raise RuntimeError("现有持仓缺报价，停止发布: " + ",".join(missing_holding_quotes))

    payload = {
        "schema_version": 1,
        "as_of": as_of,
        "candidate_source": candidate_source,
        "candidate_count": candidate_count,
        "verified_code_count": len(output_rows),
        "complete_candidate_count": complete_candidates,
        "complete_candidate_ratio": success_ratio,
        "candidate_rows": candidate_rows,
        "rows": output_rows,
        "errors": errors,
        "data_policy": {
            "quote": "腾讯批量行情",
            "dividend_financial": "东方财富，逐股串行限速",
            "missing_data": "缺失时不补造；质量门槛失败则不发布",
        },
    }
    _atomic_write(out, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-root", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--candidate-file")
    parser.add_argument("--out", default=str(ROOT / "data" / "snapshot_current.json"))
    parser.add_argument("--delay", type=float, default=1.1)
    args = parser.parse_args()
    result = refresh(
        Path(args.upstream_root).resolve(),
        args.as_of,
        args.candidate_file,
        Path(args.out).resolve(),
        max(args.delay, 0.0),
    )
    print(f"写入快照: {args.out}")
    print(f"候选完整率: {result['complete_candidate_ratio']:.1%}")


if __name__ == "__main__":
    main()

