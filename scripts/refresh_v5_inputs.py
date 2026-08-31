"""规范化并冻结 V5 辅助输入。

源 JSON 必须包含 ``adjustment_factors``、``fundamentals``、``industries``、
``h00922`` 四组记录；采集器可独立替换，本脚本只负责审计边界和落盘。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from build_historical_universe import canonical_sha256, write_json_atomic

CSINDEX_PERF_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
SINA_QFQ_URL = "https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js"
SINA_FINANCE_URL = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"


def _iso_date(value: Any) -> str:
    text = str(value or "")[:10]
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _get_json(url: str, params: dict[str, Any], timeout: int = 30) -> Any:
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": "Mozilla/5.0 V5 research input collector"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_h00922(start_date: str, as_of: str) -> list[dict[str, Any]]:
    """从中证指数官网获取 H00922 全收益指数日线。"""
    payload = _get_json(CSINDEX_PERF_URL, {
        "indexCode": "H00922", "startDate": start_date.replace("-", ""),
        "endDate": as_of.replace("-", ""),
    })
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data if isinstance(data, list) else (data or {}).get("data") or (data or {}).get("rows") or []
    result = []
    for row in rows:
        day = _iso_date(row.get("tradeDate") or row.get("date") or row.get("tradingDay"))
        close = row.get("close") or row.get("closePrice") or row.get("indexValue")
        if day and day <= as_of and close is not None:
            result.append({"date": day, "close": float(close), "source_url": CSINDEX_PERF_URL})
    if not result:
        raise RuntimeError("中证指数官网没有返回 H00922 历史日线")
    return sorted(result, key=lambda row: row["date"])


def _sina_symbol(code: str) -> str:
    return ("sh" if str(code).startswith(("5", "6", "9")) else "sz") + str(code)


def fetch_sina_adjust_factors(code: str) -> list[dict[str, Any]]:
    """按 raw_decode 解析新浪 qfq.js，保留末尾注释而不误判 JSON。"""
    url = SINA_QFQ_URL.format(symbol=_sina_symbol(code))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                   "Referer": "https://finance.sina.com.cn/"})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8", "ignore")
    brace = text.find("{")
    if brace < 0:
        raise RuntimeError(f"新浪复权因子响应无 JSON: {code}")
    data, _ = json.JSONDecoder().raw_decode(text[brace:])
    factors = [{"date": str(row["d"])[:10], "factor": float(row["f"]), "source_url": url}
               for row in data.get("data", [])]
    if not factors or factors[0]["factor"] <= 0:
        raise RuntimeError(f"新浪没有返回 {code} 有效 qfq 因子")
    return factors


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def fetch_sina_fundamentals(code: str, as_of: str) -> list[dict[str, Any]]:
    """采集新浪利润表中的发布时间和基本每股收益；字段缺失即失败。"""
    payload = _get_json(SINA_FINANCE_URL, {
        "paperCode": _sina_symbol(code), "source": "gjzb", "type": 0,
        "page": 1, "num": 100,
    })
    report_list = (((payload.get("result") or {}).get("data") or {})
                   .get("report_list") or {})
    rows = []
    for period, report in report_list.items():
        period = str(period)
        if len(period) != 8 or period[4:] != "1231":
            continue
        raw_date = str(report.get("publish_date") or "")[:8]
        published = (f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                     if len(raw_date) == 8 else "")
        eps_item = next((item for item in report.get("data") or []
                         if item.get("item_title") == "基本每股收益"
                         and item.get("item_value") not in (None, "")), None)
        if published and published <= as_of and eps_item:
            rows.append({"code": code, "year": int(period[:4]),
                         "eps": float(eps_item["item_value"]),
                         "published_date": published, "source_url": SINA_FINANCE_URL})
    if not rows:
        raise RuntimeError(f"新浪利润表没有返回 {code} 可核验的 publish_date 和基本每股收益")
    return rows


def _merge_dividends(fundamentals: list[dict[str, Any]], code: str,
                     cache_dir: Path, as_of: str) -> list[dict[str, Any]]:
    annual_path, detail_path = cache_dir / f"dv_{code}.json", cache_dir / f"dvd_{code}.json"
    if not annual_path.exists():
        raise FileNotFoundError(f"{code} 缺少年度分红缓存")
    annual = {int(row["year"]): float(row.get("dps") or 0)
              for row in json.loads(annual_path.read_text(encoding="utf-8"))}
    if not detail_path.exists():
        years = {year for year, dps in annual.items() if dps > 0}
        if any(year - 1 in years and year - 2 in years for year in years):
            raise FileNotFoundError(f"{code} 可能进入动态池但缺少逐笔分红缓存")
        return []
    ex_dates: dict[int, list[str]] = {}
    for row in json.loads(detail_path.read_text(encoding="utf-8")):
        ex_date = str(row.get("ex_date", ""))[:10]
        if ex_date and ex_date <= as_of:
            ex_dates.setdefault(int(row["year"]), []).append(ex_date)
    merged = []
    for row in fundamentals:
        year = int(row["year"])
        if year not in annual or year not in ex_dates:
            continue
        item = dict(row)
        item["dps"] = annual[year]
        # EPS 和现金分红都已公开后，派息覆盖才可用于历史信号。
        item["published_date"] = max(str(item["published_date"]), max(ex_dates[year]))
        merged.append(item)
    return merged


def collect_from_manifest(manifest_path: Path, as_of: str, industries_path: Path,
                          start_date: str = "2015-01-01", cache_dir: Path | None = None) -> dict[str, Any]:
    """按 manifest 实际采集 V5 输入；行业文件必须来自官方分类证据。"""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    codes = list(manifest.get("codes") or [row.get("code") for row in manifest.get("records", [])])
    codes = [str(code) for code in codes if str(code).startswith(("000", "001", "002", "003",
                                                                  "300", "301", "600", "601",
                                                                  "603", "605"))]
    industries = json.loads(industries_path.read_text(encoding="utf-8"))
    if isinstance(industries, dict):
        industries = industries.get("records")
    if not isinstance(industries, list) or not industries:
        raise ValueError("必须提供 CAPCO/证监会官方分类解析记录")
    classified = {str(row.get("code")) for row in industries
                  if row.get("industry") and row.get("published_date")}
    cache_dir = cache_dir or manifest_path.parent / "backtest_cache"
    missing = []
    for code in sorted(set(codes) - classified):
        annual_path = cache_dir / f"dv_{code}.json"
        annual = json.loads(annual_path.read_text(encoding="utf-8")) if annual_path.exists() else []
        years = {int(row["year"]) for row in annual if float(row.get("dps") or 0) > 0}
        if any(year - 1 in years and year - 2 in years for year in years):
            missing.append(code)
    if missing:
        raise ValueError(f"官方行业分类缺少 {len(missing)} 只可能入池股票，拒绝生成: {missing[:5]}")
    factors, fundamentals = [], []
    for code in codes:
        factors.extend({**row, "code": code} for row in fetch_sina_adjust_factors(code)
                       if row["date"] <= as_of)
        fundamentals.extend(_merge_dividends(fetch_sina_fundamentals(code, as_of), code,
                                             cache_dir, as_of))
    return {"adjustment_factors": factors, "fundamentals": fundamentals,
            "industries": industries, "h00922": fetch_h00922(start_date, as_of)}


def build_v5_inputs(source: dict[str, Any], as_of: str,
                    attachment_paths: list[Path] | None = None,
                    attachment_hashes: dict[str, str] | None = None) -> dict[str, Any]:
    """建立时点冻结快照；日期晚于截止日的记录直接拒绝。"""
    groups = ("adjustment_factors", "fundamentals", "industries", "h00922")
    missing = [name for name in groups if not isinstance(source.get(name), list)]
    if missing:
        raise ValueError(f"V5 输入缺少列表: {', '.join(missing)}")
    normalized: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        rows = [dict(row) for row in source[group]]
        for row in rows:
            date_key = "published_date" if row.get("published_date") else "date"
            visible_date = _iso_date(row.get(date_key))
            if not visible_date:
                raise ValueError(f"{group} 记录缺少 date/published_date")
            if visible_date > as_of:
                raise ValueError(f"{group} 包含截止日之后的记录: {visible_date}")
            row[date_key] = visible_date
        normalized[group] = sorted(rows, key=lambda row: (
            str(row.get("code", "")), str(row.get("published_date") or row.get("date"))))
    strategy_nav = [{**row, "date": _iso_date(row.get("date"))}
                    for row in source.get("strategy_nav", [])]
    if any(str(row.get("date", "")) > as_of for row in strategy_nav):
        raise ValueError("strategy_nav 包含截止日之后的记录")
    normalized["strategy_nav"] = sorted(strategy_nav, key=lambda row: row["date"])
    attachments = []
    for name, sha256 in sorted((attachment_hashes or {}).items()):
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise ValueError(f"附件 {name} 的 SHA256 格式错误")
        attachments.append({"name": name, "sha256": sha256.lower()})
    for path in attachment_paths or []:
        if not path.is_file():
            raise FileNotFoundError(path)
        attachments.append({"name": path.name, "sha256": file_sha256(path)})
    payload: dict[str, Any] = {
        "schema_version": 1,
        "strategy": "v5",
        "as_of": as_of,
        "price_format": "sina_qfq_factors_with_unadjusted_cache",
        "attachments": attachments,
        "inputs": normalized,
        "hashes": {name: canonical_sha256(normalized[name]) for name in (*groups, "strategy_nav")},
    }
    payload["content_sha256"] = canonical_sha256(payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="冻结 V5 辅助输入")
    parser.add_argument("--source", type=Path, help="已采集的源 JSON（离线复现）")
    parser.add_argument("--manifest", type=Path, help="联网采集时使用的冻结 manifest")
    parser.add_argument("--industries", type=Path, help="CAPCO/证监会官方分类解析 JSON")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/backtest_cache"),
                        help="用于合并已核验年度/逐笔 DPS")
    parser.add_argument("--start-date", default="2015-01-01")
    parser.add_argument("--as-of", required=True, help="显式截止日 YYYY-MM-DD")
    parser.add_argument("--output", type=Path, default=Path("data/v5_inputs.json"))
    parser.add_argument("--attachment", action="append", type=Path, default=[])
    parser.add_argument("--strategy-nav", action="append", type=Path, default=[],
                        help="round32 或 performance.json；可重复，后者按日期覆盖前者")
    return parser.parse_args()


def load_strategy_nav(paths: list[Path], as_of: str) -> list[dict[str, Any]]:
    """按重叠日对齐历史回测与前向 NAV，避免账户切换制造虚假收益。"""
    backtest: dict[str, float] = {}
    forward: dict[str, float] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload.get("nav_series"), list):
            target = backtest
            rows = ((row.get("date"), row.get("nav")) for row in payload["nav_series"])
        elif isinstance(payload.get("series"), list):
            target = forward
            rows = ((row.get("date"), row.get("v5_nav")) for row in payload["series"])
        else:
            raise ValueError(f"strategy-nav 格式不支持: {path}")
        for day, nav in rows:
            day = str(day or "")[:10]
            if day and day <= as_of and nav is not None:
                target[day] = float(nav)
    merged = dict(backtest)
    if backtest:
        anchor = min(forward) if forward else max(backtest)
        base_day = max((day for day in backtest if day <= anchor), default=max(backtest))
        target_nav = forward.get(anchor, 100_000.0)
        scale = target_nav / backtest[base_day]
        merged = {day: nav * scale for day, nav in backtest.items()}
    merged.update(forward)
    return [{"date": day, "nav": merged[day]} for day in sorted(merged)]


def main() -> None:
    args = parse_args()
    if args.source:
        source = json.loads(args.source.read_text(encoding="utf-8"))
    elif args.manifest and args.industries:
        source = collect_from_manifest(args.manifest, args.as_of, args.industries,
                                       args.start_date, args.cache_dir)
    else:
        raise SystemExit("必须指定 --source，或同时指定 --manifest 与 --industries")
    if args.strategy_nav:
        source["strategy_nav"] = load_strategy_nav(args.strategy_nav, args.as_of)
    from v5_strategy import V5_ATTACHMENT_SHA256
    if args.attachment:
        actual = {"report_pdf": next((file_sha256(path) for path in args.attachment
                                      if path.suffix.lower() == ".pdf"), None),
                  "appendix_xlsx": next((file_sha256(path) for path in args.attachment
                                         if path.suffix.lower() == ".xlsx"), None)}
        if actual != V5_ATTACHMENT_SHA256:
            raise ValueError("两份 V5 附件 SHA256 与冻结合同不一致")
    write_json_atomic(args.output, build_v5_inputs(
        source, args.as_of, attachment_hashes=V5_ATTACHMENT_SHA256
    ))


if __name__ == "__main__":
    main()
