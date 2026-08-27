"""采集并交叉核验在市股票的历史分红。

东财是覆盖全部在市股票的主数据源，BaoStock 或新浪独立核验可能进入冻结 V1
决策路径的候选。采集过程按股票写入原子断点，候选事件逐项一致后才允许放行。
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import random
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import requests

from build_historical_universe import ROOT, canonical_sha256, write_json_atomic
from refresh_backtest_cache import _normalize_dividend_rows


DEFAULT_MASTER = ROOT / "data" / "historical" / "security_master.json"
DEFAULT_CHECKPOINT_DIR = ROOT / "data" / "historical" / "checkpoints" / "listed_dividends"
DEFAULT_OUTPUT = ROOT / "data" / "historical" / "listed_dividends.json"
DEFAULT_SELECTION_SCOPE = ROOT / "data" / "historical" / "selection_relevant_scope.json"
VERIFICATION_START_DATE = "2012-01-01"
EASTMONEY_ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SINA_URL = (
    "https://vip.stock.finance.sina.com.cn/corp/go.php/"
    "vISSUE_ShareBonus/stockid/{code}.phtml"
)
VERIFICATION_SOURCES = {
    "baostock": "baostock/query_dividend_data",
    "sina": "sina/vISSUE_ShareBonus",
    "tonghuashun": "tonghuashun/F10 bonus_table",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def listed_targets(master: dict[str, Any], as_of: str) -> list[dict[str, Any]]:
    """固定截止日内仍在市的沪深 A 股目标集合。"""
    records = [
        row for row in master.get("records", [])
        if row.get("status") == "listed"
        and row.get("list_date")
        and row["list_date"] <= as_of
    ]
    return sorted(records, key=lambda row: row["code"])


class SerialHttpClient:
    """同一进程内串行限流，确保相邻请求满足最小间隔。"""

    def __init__(
        self,
        min_interval: float,
        jitter: float,
        *,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        random_delay: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.min_interval = max(float(min_interval), 0.0)
        self.jitter = max(float(jitter), 0.0)
        self.session = session or requests.Session()
        self.clock = clock
        self.sleeper = sleeper
        self.random_delay = random_delay
        self._last_request_at: float | None = None

    def get(self, *args: Any, **kwargs: Any) -> requests.Response:
        if self._last_request_at is not None:
            target = self.min_interval + self.random_delay(0.0, self.jitter)
            remaining = target - (self.clock() - self._last_request_at)
            if remaining > 0:
                self.sleeper(remaining)
        response = self.session.get(*args, **kwargs)
        self._last_request_at = self.clock()
        return response


def fetch_eastmoney_dividends(
    code: str,
    as_of: str,
    client: SerialHttpClient,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """拉取东财全部分页，并校验分页总数，空结果显式留痕。"""
    raw_rows: list[dict[str, Any]] = []
    raw_pages: list[dict[str, Any]] = []
    page = 1
    expected_count: int | None = None
    while True:
        response = client.get(
            EASTMONEY_ENDPOINT,
            params={
                "reportName": "RPT_SHAREBONUS_DET",
                "columns": "ALL",
                "filter": f'(SECURITY_CODE="{code}")',
                "pageNumber": page,
                "pageSize": 100,
                "sortColumns": "REPORT_DATE",
                "sortTypes": "-1",
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://data.eastmoney.com/",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("code")) == "9201" and "返回数据为空" in str(payload.get("message")):
            if page != 1:
                raise RuntimeError("东财分页中途返回明确空结果")
            return {
                "provider": "eastmoney/RPT_SHAREBONUS_DET",
                "provider_complete": True,
                "empty_response": True,
                "raw_record_count": 0,
                "raw_response_sha256": canonical_sha256([payload]),
                "records": [],
            }
        if not payload.get("success") or str(payload.get("code", "0")) != "0":
            raise RuntimeError(f"东财响应拒绝: code={payload.get('code')} msg={payload.get('message')}")
        result = payload.get("result")
        if result is None:
            if page != 1:
                raise RuntimeError("东财分页中途返回空 result")
            expected_count = 0
            raw_pages.append(payload)
            break
        if not isinstance(result, dict):
            raise RuntimeError("东财 result 结构异常")
        data = result.get("data")
        count = int(result.get("count") or 0)
        pages = int(result.get("pages") or 1)
        if data is None and count == 0:
            data = []
        if not isinstance(data, list):
            raise RuntimeError("东财 data 结构异常")
        if expected_count is None:
            expected_count = count
        elif count != expected_count:
            raise RuntimeError("东财分页总数在请求期间发生变化")
        raw_rows.extend(data)
        raw_pages.append(payload)
        if page >= pages:
            break
        page += 1
    if len(raw_rows) != expected_count:
        raise RuntimeError(f"东财分页条数不完整: expected={expected_count}, actual={len(raw_rows)}")
    records = _normalize_dividend_rows(raw_rows, as_of)
    return {
        "provider": "eastmoney/RPT_SHAREBONUS_DET",
        "provider_complete": True,
        "empty_response": expected_count == 0,
        "raw_record_count": len(raw_rows),
        "raw_response_sha256": canonical_sha256(raw_pages),
        "records": records,
    }


class _DividendTableParser(HTMLParser):
    def __init__(self, table_id: str = "sharebonus_1") -> None:
        super().__init__()
        self.table_id = table_id
        self.in_table = False
        self.table_found = False
        self.in_cell = False
        self.current_cell: list[str] = []
        self.current_row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table" and attributes.get("id") == self.table_id:
            self.in_table = True
            self.table_found = True
        elif self.in_table and tag == "tr":
            self.current_row = []
        elif self.in_table and tag == "td":
            self.in_cell = True
            self.current_cell = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.in_table and tag == "td" and self.in_cell:
            self.current_row.append("".join(self.current_cell).strip())
            self.in_cell = False
        elif self.in_table and tag == "tr":
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = []
        elif self.in_table and tag == "table":
            self.in_table = False


def _nonnegative_number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(number, 0.0), 6)


def parse_sina_dividends(html: str, as_of: str) -> list[dict[str, Any]]:
    """解析新浪分红表，只保留截止日内已经实施的事件。"""
    if "除权除息日" not in html or "分红方案(每10股)" not in html:
        raise RuntimeError("新浪分红页缺少预期表头")
    parser = _DividendTableParser()
    parser.feed(html)
    if not parser.table_found:
        raise RuntimeError("新浪分红页缺少 sharebonus_1 表")
    records = []
    for cells in parser.rows:
        if len(cells) < 8:
            continue
        announce_date, bonus, transfer, cash, progress, ex_date, registration_date = cells[:7]
        if "实施" not in progress and "完成" not in progress:
            continue
        ex_date = ex_date[:10]
        if len(ex_date) != 10 or ex_date > as_of:
            continue
        records.append({
            "plan_announce_date": announce_date[:10],
            "registration_date": registration_date[:10],
            "ex_date": ex_date,
            "dps": round(_nonnegative_number(cash) / 10.0, 6),
            "bonus_ratio": _nonnegative_number(bonus),
            "transfer_ratio": _nonnegative_number(transfer),
        })
    return sorted(
        records,
        key=lambda row: (row["ex_date"], row["dps"], row["bonus_ratio"], row["transfer_ratio"]),
    )


def fetch_sina_dividends(
    code: str,
    as_of: str,
    client: SerialHttpClient,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    url = SINA_URL.format(code=code)
    response = client.get(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        timeout=timeout,
    )
    response.raise_for_status()
    response.encoding = "gbk"
    html = response.text
    if code not in html[:10000]:
        raise RuntimeError("新浪页面未包含目标股票代码")
    records = parse_sina_dividends(html, as_of)
    return {
        "provider": "sina/vISSUE_ShareBonus",
        "provider_complete": True,
        "empty_response": not records,
        "source_page_sha256": hashlib.sha256(response.content).hexdigest(),
        "records": records,
    }


def parse_tonghuashun_dividends(html: str, as_of: str) -> list[dict[str, Any]]:
    """解析同花顺 F10 分红融资页的已实施分红事件。"""
    if "A股除权除息日" not in html or "分红方案说明" not in html:
        raise RuntimeError("同花顺分红页缺少预期表头")
    parser = _DividendTableParser("bonus_table")
    parser.feed(html)
    if not parser.table_found:
        raise RuntimeError("同花顺分红页缺少 bonus_table 表")
    records = []
    for cells in parser.rows:
        if len(cells) < 9:
            continue
        _, _, _, implementation_date, plan, registration_date, ex_date, _, progress = cells[:9]
        if "实施" not in progress:
            continue
        ex_date = ex_date[:10]
        if len(ex_date) != 10 or ex_date > as_of:
            continue

        def plan_number(pattern: str) -> float:
            match = re.search(pattern, plan)
            return _nonnegative_number(match.group(1)) if match else 0.0

        dps = round(plan_number(r"派\s*([\d.]+)\s*元") / 10.0, 6)
        bonus_ratio = plan_number(r"送\s*([\d.]+)\s*股")
        transfer_ratio = plan_number(r"转(?:增)?\s*([\d.]+)\s*股")
        if dps == 0 and bonus_ratio == 0 and transfer_ratio == 0:
            continue
        records.append({
            "plan_announce_date": implementation_date[:10],
            "registration_date": registration_date[:10],
            "ex_date": ex_date,
            "dps": dps,
            "bonus_ratio": bonus_ratio,
            "transfer_ratio": transfer_ratio,
        })
    return sorted(
        records,
        key=lambda row: (
            row["ex_date"], row["dps"], row["bonus_ratio"], row["transfer_ratio"]
        ),
    )


def fetch_tonghuashun_dividends(
    code: str,
    as_of: str,
    client: SerialHttpClient,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    response = client.get(
        f"https://basic.10jqka.com.cn/{code}/bonus.html",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://basic.10jqka.com.cn/{code}/",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    response.encoding = "gbk"
    html = response.text
    if code not in html[:10000]:
        raise RuntimeError("同花顺页面未包含目标股票代码")
    records = parse_tonghuashun_dividends(html, as_of)
    return {
        "provider": "tonghuashun/F10 bonus_table",
        "provider_complete": True,
        "empty_response": not records,
        "source_page_sha256": hashlib.sha256(response.content).hexdigest(),
        "records": records,
    }


def _baostock_code(code: str) -> str:
    return ("sh." if str(code).startswith("6") else "sz.") + str(code).zfill(6)


def normalize_baostock_dividend(row: dict[str, Any], as_of: str) -> dict[str, Any] | None:
    """转为项目事件口径；BaoStock 送转字段为每股，转成每10股。"""
    ex_date = str(row.get("dividOperateDate") or "")[:10]
    if len(ex_date) != 10 or ex_date > as_of:
        return None
    return {
        "plan_announce_date": str(row.get("dividPlanAnnounceDate") or "")[:10],
        "registration_date": str(row.get("dividRegistDate") or "")[:10],
        "ex_date": ex_date,
        "pay_date": str(row.get("dividPayDate") or "")[:10],
        "dps": _nonnegative_number(row.get("dividCashPsBeforeTax")),
        "bonus_ratio": round(_nonnegative_number(row.get("dividStocksPs")) * 10.0, 6),
        "transfer_ratio": round(_nonnegative_number(row.get("dividReserveToStockPs")) * 10.0, 6),
    }


class BaoStockDividendClient:
    """单登录、逐股票逐报告年度串行查询 BaoStock。"""

    def __init__(self, *, interval: float = 0.02, sleeper: Callable[[float], None] = time.sleep):
        import baostock as bs

        self.bs = bs
        self.interval = max(float(interval), 0.0)
        self.sleeper = sleeper
        login = bs.login()
        if getattr(login, "error_code", "") != "0":
            raise RuntimeError(f"BaoStock 登录失败: {getattr(login, 'error_msg', '')}")

    def close(self) -> None:
        self.bs.logout()

    def fetch(
        self, code: str, as_of: str, list_date: str,
        query_years: list[int] | None = None,
    ) -> dict[str, Any]:
        start_year = max(int(VERIFICATION_START_DATE[:4]), int(str(list_date)[:4]))
        end_year = int(as_of[:4])
        raw_rows = []
        requested_years = (
            query_years if query_years is not None else range(start_year, end_year + 1)
        )
        queried_years = sorted({
            int(year) for year in requested_years
            if start_year <= int(year) <= end_year
        })
        if not queried_years:
            return {
                "provider": "baostock/query_dividend_data",
                "provider_complete": True,
                "empty_response": True,
                "queried_years": [],
                "raw_record_count": 0,
                "raw_response_sha256": canonical_sha256([]),
                "records": [],
            }
        for index, year in enumerate(queried_years):
            result = self.bs.query_dividend_data(
                code=_baostock_code(code), year=str(year), yearType="report"
            )
            if getattr(result, "error_code", "") != "0":
                raise RuntimeError(
                    f"BaoStock 分红查询失败: code={code} year={year} "
                    f"error={getattr(result, 'error_code', '')} {getattr(result, 'error_msg', '')}"
                )
            while result.next():
                raw_rows.append(dict(zip(result.fields, result.get_row_data())))
            if index + 1 < len(queried_years) and self.interval:
                self.sleeper(self.interval)
        records = []
        for row in raw_rows:
            normalized = normalize_baostock_dividend(row, as_of)
            if normalized is not None:
                records.append(normalized)
        # report_year 可能错位，不参与事件身份；完全重复事件只保留一次。
        unique = {canonical_sha256(row): row for row in records}
        records = sorted(
            unique.values(),
            key=lambda row: (row["ex_date"], row["dps"], row["bonus_ratio"], row["transfer_ratio"]),
        )
        return {
            "provider": "baostock/query_dividend_data",
            "provider_complete": True,
            "empty_response": not records,
            "queried_years": queried_years,
            "raw_record_count": len(raw_rows),
            "raw_response_sha256": canonical_sha256(raw_rows),
            "records": records,
        }


def max_consecutive_positive_years(records: list[dict[str, Any]]) -> int:
    years = sorted({
        int(row["year"])
        for row in records
        if str(row.get("year") or "").isdigit() and _nonnegative_number(row.get("dps")) > 0
    })
    best = current = 0
    previous: int | None = None
    for year in years:
        current = current + 1 if previous is not None and year == previous + 1 else 1
        best = max(best, current)
        previous = year
    return best


def eligible_primary_scope(
    targets: list[dict[str, Any]], checkpoint_dir: Path, as_of: str,
    *, min_years: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, list[int]]]:
    """仅按完整东财主源历史确定可能进入动态池的安全上界。"""
    eligible = []
    query_years: dict[str, list[int]] = {}
    incomplete = []
    for stock in targets:
        path = checkpoint_path(checkpoint_dir, "eastmoney", stock["code"])
        payload = read_json(path) if path.exists() else {}
        if not checkpoint_is_complete(payload, "eastmoney", stock["code"], as_of):
            incomplete.append(stock["code"])
            continue
        records = payload.get("records", [])
        if max_consecutive_positive_years(records) < min_years:
            continue
        years = set()
        for row in records:
            ex_date = str(row.get("ex_date") or "")[:10]
            if len(ex_date) != 10 or ex_date < VERIFICATION_START_DATE or ex_date > as_of:
                continue
            ex_year = int(ex_date[:4])
            years.update((ex_year - 1, ex_year, ex_year + 1))
        eligible.append(stock)
        query_years[stock["code"]] = sorted(years)
    if incomplete:
        raise RuntimeError(
            f"东财主源尚未全量完成: complete={len(targets) - len(incomplete)}/"
            f"{len(targets)}，拒绝提前筛选 BaoStock 核验范围"
        )
    return eligible, query_years


def checkpoint_path(base: Path, provider: str, code: str) -> Path:
    return base / provider / f"{code}.json"


def checkpoint_is_complete(
    payload: dict[str, Any], provider: str, code: str, as_of: str,
) -> bool:
    records = payload.get("records")
    return (
        payload.get("schema_version") == 1
        and payload.get("provider") == provider
        and payload.get("code") == code
        and payload.get("as_of") == as_of
        and payload.get("provider_complete") is True
        and isinstance(records, list)
        and payload.get("records_sha256") == canonical_sha256(records)
    )


def checkpoint_attempt_is_current(
    payload: dict[str, Any], provider: str, code: str, as_of: str,
) -> bool:
    """成功或失败断点都算已尝试，但必须绑定当前代码、截止日和内容哈希。"""
    records = payload.get("records")
    return (
        payload.get("schema_version") == 1
        and payload.get("provider") == provider
        and payload.get("code") == code
        and payload.get("as_of") == as_of
        and isinstance(records, list)
        and payload.get("records_sha256") == canonical_sha256(records)
        and (
            payload.get("provider_complete") is True
            or bool(str(payload.get("error") or "").strip())
        )
    )


def primary_records_scope_sha256(
    targets: list[dict[str, Any]], checkpoint_dir: Path, as_of: str,
) -> str:
    fingerprints = []
    for stock in targets:
        path = checkpoint_path(checkpoint_dir, "eastmoney", stock["code"])
        payload = read_json(path) if path.exists() else {}
        if not checkpoint_is_complete(payload, "eastmoney", stock["code"], as_of):
            raise RuntimeError(f"东财主源断点不完整: {stock['code']}")
        fingerprints.append({
            "code": stock["code"],
            "records_sha256": payload["records_sha256"],
        })
    return canonical_sha256(fingerprints)


def collect_provider(
    targets: list[dict[str, Any]],
    checkpoint_dir: Path,
    provider: str,
    as_of: str,
    fetcher: Callable[[str, str], dict[str, Any]],
    *,
    retry: int = 2,
    limit: int = 0,
    max_consecutive_failures: int = 5,
) -> dict[str, int]:
    """按股票断点续采；失败断点会在下次运行时自动重试。"""
    attempted = skipped = succeeded = failed = 0
    consecutive_failures = 0
    for index, stock in enumerate(targets, 1):
        path = checkpoint_path(checkpoint_dir, provider, stock["code"])
        if path.exists():
            try:
                existing = read_json(path)
            except (OSError, json.JSONDecodeError):
                existing = {}
            if checkpoint_is_complete(existing, provider, stock["code"], as_of):
                skipped += 1
                continue
        if limit and attempted >= limit:
            break
        attempted += 1
        error = ""
        result: dict[str, Any] = {}
        for _ in range(max(retry, 0) + 1):
            try:
                result = fetcher(stock["code"], as_of)
                error = ""
                break
            except Exception as exc:  # 网络错误必须落盘，不能伪装成空结果
                error = f"{type(exc).__name__}: {exc}"
        records = result.get("records", []) if not error else []
        payload = {
            "schema_version": 1,
            "provider": provider,
            "code": stock["code"],
            "name": stock["name"],
            "list_date": stock["list_date"],
            "as_of": as_of,
            "provider_complete": bool(result.get("provider_complete")) and not error,
            "empty_response": bool(result.get("empty_response")) if not error else False,
            "error": error,
            "records": records,
            "records_sha256": canonical_sha256(records),
            "source_metadata": {
                key: value for key, value in result.items()
                if key not in {"provider", "provider_complete", "empty_response", "records"}
            },
        }
        write_json_atomic(path, payload)
        if payload["provider_complete"]:
            succeeded += 1
            consecutive_failures = 0
        else:
            failed += 1
            consecutive_failures += 1
        if attempted % 25 == 0 or index == len(targets):
            print(
                f"{provider} 进度 {index}/{len(targets)}，"
                f"新完成 {succeeded}，失败 {failed}，跳过 {skipped}"
            )
        if (
            max_consecutive_failures > 0
            and consecutive_failures >= max_consecutive_failures
        ):
            raise RuntimeError(
                f"{provider} 连续 {consecutive_failures} 只采集失败，"
                "可能已被限流或服务中断，已停止继续请求"
            )
    return {
        "attempted": attempted,
        "skipped": skipped,
        "succeeded": succeeded,
        "failed": failed,
    }


def _event_projection(
    records: list[dict[str, Any]], start_date: str = VERIFICATION_START_DATE,
) -> list[dict[str, Any]]:
    projected = []
    for row in records:
        ex_date = str(row.get("ex_date") or "")[:10]
        if ex_date < start_date:
            continue
        projected.append({
            "ex_date": ex_date,
            # 冻结 V1 的东财缓存以每股 4 位小数计算；新浪网页可能多展示 1-2 位。
            "dps": round(_nonnegative_number(row.get("dps")), 4),
            "bonus_ratio": round(_nonnegative_number(row.get("bonus_ratio")), 6),
            "transfer_ratio": round(_nonnegative_number(row.get("transfer_ratio")), 6),
        })
    return sorted(
        projected,
        key=lambda row: (row["ex_date"], row["dps"], row["bonus_ratio"], row["transfer_ratio"]),
    )


def _counter_rows(counter: Counter[str], rows_by_key: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        rows_by_key[key]
        for key in sorted(counter)
        for _ in range(counter[key])
    ]


def build_verified_artifact(
    targets: list[dict[str, Any]], checkpoint_dir: Path, as_of: str,
    selection_relevant_codes: set[str] | None = None,
) -> dict[str, Any]:
    stocks = []
    for stock in targets:
        primary_path = checkpoint_path(checkpoint_dir, "eastmoney", stock["code"])
        primary = read_json(primary_path) if primary_path.exists() else {}
        primary_ok = checkpoint_is_complete(primary, "eastmoney", stock["code"], as_of)
        primary_events = _event_projection(primary.get("records", [])) if primary_ok else []
        primary_candidate = (
            primary_ok
            and max_consecutive_positive_years(primary.get("records", [])) >= 3
        )
        verification_required = primary_candidate and (
            selection_relevant_codes is None or stock["code"] in selection_relevant_codes
        )
        primary_keys = {canonical_sha256(row): row for row in primary_events}
        primary_counter = Counter(canonical_sha256(row) for row in primary_events)
        primary_duplicate_count = len(primary_events) - len(primary_counter)
        verifier_options = []
        if verification_required:
            for provider in VERIFICATION_SOURCES:
                path = checkpoint_path(checkpoint_dir, provider, stock["code"])
                verifier = read_json(path) if path.exists() else {}
                attempted = checkpoint_attempt_is_current(
                    verifier, provider, stock["code"], as_of
                )
                complete = checkpoint_is_complete(
                    verifier, provider, stock["code"], as_of
                )
                events = _event_projection(verifier.get("records", [])) if complete else []
                counter = Counter(canonical_sha256(row) for row in events)
                duplicate_count = len(events) - len(counter)
                verifier_options.append({
                    "provider": provider,
                    "payload": verifier,
                    "attempted": attempted,
                    "complete": complete,
                    "events": events,
                    "counter": counter,
                    "duplicate_count": duplicate_count,
                    "exact_match": (
                        primary_ok
                        and complete
                        and primary_counter == counter
                        and primary_duplicate_count == 0
                        and duplicate_count == 0
                    ),
                })
        verifier_option = next(
            (item for item in verifier_options if item["exact_match"]),
            next(
                (item for item in verifier_options if item["complete"]),
                next(
                    (item for item in verifier_options if item["attempted"]),
                    {
                        "provider": "baostock", "payload": {}, "attempted": False,
                        "complete": False, "events": [], "counter": Counter(),
                        "duplicate_count": 0, "exact_match": False,
                    },
                ),
            ),
        )
        verifier_provider = verifier_option["provider"]
        verifier = verifier_option["payload"]
        verification_attempted = any(
            item["attempted"] for item in verifier_options
        )
        verifier_ok = verifier_option["complete"]
        verifier_events = verifier_option["events"]
        verifier_counter = verifier_option["counter"]
        verifier_duplicate_count = verifier_option["duplicate_count"]
        verifier_keys = {canonical_sha256(row): row for row in verifier_events}
        exact_match = verifier_option["exact_match"]
        stocks.append({
            "code": stock["code"],
            "name": stock["name"],
            "list_date": stock["list_date"],
            "delist_date": stock.get("delist_date", ""),
            "records": primary.get("records", []) if primary_ok else [],
            "records_sha256": primary.get("records_sha256", canonical_sha256([])),
            "primary_provider_complete": primary_ok,
            "verification_provider_complete": verifier_ok,
            "verification_attempted": verification_attempted,
            "verification_required": verification_required,
            "verification_not_required": primary_ok and not verification_required,
            "primary_candidate": primary_candidate,
            "verification_filter_reason": (
                "东财主源历史未出现连续3年正现金分红，不可能进入冻结V1动态池"
                if primary_ok and not primary_candidate
                else "冻结V1历史信号路径从未同时满足动态池、入场股息率和动量门槛"
                if primary_candidate and not verification_required else ""
            ),
            "independently_verified": exact_match,
            "data_quality_eligible": exact_match,
            "filtered_unverifiable": verification_required and verification_attempted and not exact_match,
            "data_quality_filter_reason": (
                "独立分红核验请求失败，人工数据门禁不放行"
                if verification_required and verification_attempted and not verifier_ok
                else "东财与独立来源事件未形成无重复的一一匹配，人工数据门禁不放行"
                if verification_required and verification_attempted and not exact_match else ""
            ),
            "verification": {
                "provider": VERIFICATION_SOURCES[verifier_provider],
                "provider_key": verifier_provider,
                "exact_match": exact_match,
                "primary_event_count": len(primary_events),
                "verification_event_count": len(verifier_events),
                "primary_duplicate_event_count": primary_duplicate_count,
                "verification_duplicate_event_count": verifier_duplicate_count,
                "primary_events_sha256": canonical_sha256(primary_events),
                "verification_events_sha256": canonical_sha256(verifier_events),
                "missing_from_verifier": _counter_rows(
                    primary_counter - verifier_counter, primary_keys
                ),
                "missing_from_eastmoney": _counter_rows(
                    verifier_counter - primary_counter, verifier_keys
                ),
                "raw_response_sha256": (verifier.get("source_metadata") or {}).get(
                    "raw_response_sha256", ""
                ),
            },
        })
    primary_provider_complete = all(item["primary_provider_complete"] for item in stocks)
    primary_candidates = [item for item in stocks if item["primary_candidate"]]
    eligible_stocks = [item for item in stocks if item["verification_required"]]
    eligible_scope_independently_verified = bool(eligible_stocks) and all(
        item["independently_verified"] for item in eligible_stocks
    )
    manual_data_gate_complete = (
        selection_relevant_codes is not None
        and bool(eligible_stocks)
        and all(item["verification_attempted"] for item in eligible_stocks)
    )
    data_quality_eligible_codes = sorted(
        item["code"] for item in eligible_stocks if item["data_quality_eligible"]
    )
    payload = {
        "schema_version": 1,
        "source_snapshot_date": as_of,
        "scope": (
            "截至截止日仍在市的沪深 A 股；主记录保留截止日前全部已实施事件；"
            f"独立核验覆盖冻结 V1 所需的 {VERIFICATION_START_DATE} 起事件"
        ),
        "verification_start_date": VERIFICATION_START_DATE,
        "primary_source": "eastmoney/RPT_SHAREBONUS_DET",
        "verification_sources": list(VERIFICATION_SOURCES.values()),
        "target_count": len(targets),
        "target_codes_sha256": canonical_sha256([stock["code"] for stock in targets]),
        "primary_provider_complete": primary_provider_complete,
        "primary_eligible_scope_count": len(primary_candidates),
        "eligible_scope_count": len(eligible_stocks),
        "selection_relevant_count": len(eligible_stocks),
        "selection_relevant_codes": [item["code"] for item in eligible_stocks],
        "eligible_scope_independently_verified": eligible_scope_independently_verified,
        "decision_path_verified": eligible_scope_independently_verified,
        "decision_path_resolved": manual_data_gate_complete,
        "manual_data_gate_complete": manual_data_gate_complete,
        "manual_data_gate_status": (
            "complete_with_exclusions" if manual_data_gate_complete else "incomplete"
        ),
        "data_quality_eligible_count": len(data_quality_eligible_codes),
        "data_quality_eligible_codes": data_quality_eligible_codes,
        "data_quality_eligible_codes_sha256": canonical_sha256(data_quality_eligible_codes),
        "filtered_unverifiable_count": sum(
            item["filtered_unverifiable"] for item in eligible_stocks
        ),
        "filtered_non_candidate_count": sum(
            item["primary_provider_complete"] and not item["primary_candidate"] for item in stocks
        ),
        "filtered_not_selection_relevant_count": sum(
            item["primary_candidate"] and not item["verification_required"] for item in stocks
        ),
        # 兼容旧读取方；语义现在明确限定为主源全量与候选范围核验。
        "provider_complete": primary_provider_complete,
        "independently_verified": eligible_scope_independently_verified,
        "verified_stock_count": sum(item["independently_verified"] for item in eligible_stocks),
        "mismatched_stock_count": sum(
            item["verification_required"]
            and item["primary_provider_complete"]
            and item["verification_provider_complete"]
            and not item["independently_verified"]
            for item in stocks
        ),
        "failed_stock_count": sum(
            not item["primary_provider_complete"]
            or (item["verification_required"] and not item["verification_provider_complete"])
            for item in stocks
        ),
        "record_count": sum(len(item["records"]) for item in stocks),
        "stocks": stocks,
        "limitations": [
            "东财与 BaoStock 或新浪一致是独立交叉核验，不等同于交易所公告逐事件确认。",
            "退市股票仍由独立流水线处理，本产物只覆盖截止日仍在市股票。",
            "人工数据门禁会排除双源请求失败或事件无法一一匹配的股票；该过滤不使用回测收益。",
            "因此 complete_with_exclusions 只表示门禁处理完成，不代表全市场分红都已独立核验。",
        ],
    }
    payload["stocks_sha256"] = canonical_sha256(stocks)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=("eastmoney", "sina", "tonghuashun", "baostock", "both", "build"),
        required=True,
    )
    parser.add_argument("--as-of", required=True, help="冻结输入截止日 YYYY-MM-DD")
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--checkpoint-dir", type=Path, default=DEFAULT_CHECKPOINT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--selection-scope", type=Path, default=DEFAULT_SELECTION_SCOPE)
    parser.add_argument("--retry", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0, help="仅处理指定数量的新断点，0 表示全部")
    parser.add_argument("--eastmoney-interval", type=float, default=1.05)
    parser.add_argument("--sina-interval", type=float, default=0.20)
    parser.add_argument("--tonghuashun-interval", type=float, default=0.10)
    parser.add_argument("--baostock-interval", type=float, default=0.02)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    master = read_json(args.master)
    targets = listed_targets(master, args.as_of)
    if not targets:
        raise RuntimeError("在市股票目标集合为空")
    selection_scope = None
    if args.provider in {"sina", "tonghuashun", "baostock", "both", "build"}:
        if not args.selection_scope.exists():
            raise RuntimeError("缺少 selection_relevant_scope.json，拒绝继续决策路径核验")
        selection_scope = read_json(args.selection_scope)
        if (
            selection_scope.get("status") not in {
                "complete", "complete_with_exclusions"
            }
            or selection_scope.get("manual_data_gate_complete") is not True
            or selection_scope.get("as_of") != args.as_of
        ):
            raise RuntimeError("selection_relevant_scope 状态或截止日不匹配")
        expected_records_hash = primary_records_scope_sha256(
            targets, args.checkpoint_dir, args.as_of
        )
        if selection_scope.get("listed_dividend_records_sha256") != expected_records_hash:
            raise RuntimeError("selection_relevant_scope 与当前东财分红断点哈希不匹配")
    if args.provider in {"eastmoney", "both"}:
        eastmoney_client = SerialHttpClient(args.eastmoney_interval, 0.25)
        result = collect_provider(
            targets,
            args.checkpoint_dir,
            "eastmoney",
            args.as_of,
            lambda code, as_of: fetch_eastmoney_dividends(code, as_of, eastmoney_client),
            retry=args.retry,
            limit=args.limit,
        )
        print(json.dumps({"eastmoney": result}, ensure_ascii=False))
    if args.provider == "sina":
        eligible, _ = eligible_primary_scope(
            targets, args.checkpoint_dir, args.as_of
        )
        assert selection_scope is not None
        selected_codes = set(selection_scope.get("selection_relevant_codes", []))
        eligible = [stock for stock in eligible if stock["code"] in selected_codes]
        pending = []
        for stock in eligible:
            path = checkpoint_path(args.checkpoint_dir, "baostock", stock["code"])
            verifier = read_json(path) if path.exists() else {}
            if not checkpoint_is_complete(
                verifier, "baostock", stock["code"], args.as_of
            ):
                pending.append(stock)
        sina_client = SerialHttpClient(args.sina_interval, 0.10)
        result = collect_provider(
            pending,
            args.checkpoint_dir,
            "sina",
            args.as_of,
            lambda code, as_of: fetch_sina_dividends(code, as_of, sina_client),
            retry=args.retry,
            limit=args.limit,
        )
        print(json.dumps({"sina": result}, ensure_ascii=False))
    if args.provider == "tonghuashun":
        eligible, _ = eligible_primary_scope(
            targets, args.checkpoint_dir, args.as_of
        )
        assert selection_scope is not None
        selected_codes = set(selection_scope.get("selection_relevant_codes", []))
        eligible = [stock for stock in eligible if stock["code"] in selected_codes]
        pending = []
        for stock in eligible:
            has_complete_verifier = False
            for provider in ("baostock", "sina"):
                path = checkpoint_path(args.checkpoint_dir, provider, stock["code"])
                verifier = read_json(path) if path.exists() else {}
                if checkpoint_is_complete(
                    verifier, provider, stock["code"], args.as_of
                ):
                    has_complete_verifier = True
                    break
            if not has_complete_verifier:
                pending.append(stock)
        tonghuashun_client = SerialHttpClient(
            args.tonghuashun_interval, 0.05
        )
        result = collect_provider(
            pending,
            args.checkpoint_dir,
            "tonghuashun",
            args.as_of,
            lambda code, as_of: fetch_tonghuashun_dividends(
                code, as_of, tonghuashun_client
            ),
            retry=args.retry,
            limit=args.limit,
        )
        print(json.dumps({"tonghuashun": result}, ensure_ascii=False))
    if args.provider in {"baostock", "both"}:
        eligible, query_years = eligible_primary_scope(
            targets, args.checkpoint_dir, args.as_of
        )
        assert selection_scope is not None
        selected_codes = set(selection_scope.get("selection_relevant_codes", []))
        eligible = [stock for stock in eligible if stock["code"] in selected_codes]
        query_years = {stock["code"]: query_years[stock["code"]] for stock in eligible}
        list_dates = {stock["code"]: stock["list_date"] for stock in eligible}
        baostock_client = BaoStockDividendClient(interval=args.baostock_interval)
        try:
            result = collect_provider(
                eligible,
                args.checkpoint_dir,
                "baostock",
                args.as_of,
                lambda code, as_of: baostock_client.fetch(
                    code, as_of, list_dates[code], query_years[code]
                ),
                retry=args.retry,
                limit=args.limit,
            )
        finally:
            baostock_client.close()
        print(json.dumps({"baostock": result}, ensure_ascii=False))
    if args.provider in {"build", "both"}:
        assert selection_scope is not None
        selected_codes = set(selection_scope.get("selection_relevant_codes", []))
        payload = build_verified_artifact(
            targets, args.checkpoint_dir, args.as_of, selected_codes
        )
        write_json_atomic(args.output, payload)
        print(json.dumps({
            "output": str(args.output),
            "target_count": payload["target_count"],
            "primary_provider_complete": payload["primary_provider_complete"],
            "eligible_scope_count": payload["eligible_scope_count"],
            "eligible_scope_independently_verified": payload[
                "eligible_scope_independently_verified"
            ],
            "manual_data_gate_complete": payload["manual_data_gate_complete"],
            "data_quality_eligible_count": payload["data_quality_eligible_count"],
            "filtered_unverifiable_count": payload["filtered_unverifiable_count"],
            "filtered_non_candidate_count": payload["filtered_non_candidate_count"],
            "verified_stock_count": payload["verified_stock_count"],
            "mismatched_stock_count": payload["mismatched_stock_count"],
            "failed_stock_count": payload["failed_stock_count"],
        }, ensure_ascii=False, indent=2))
        if (
            not payload["primary_provider_complete"]
            or not payload["manual_data_gate_complete"]
        ):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
