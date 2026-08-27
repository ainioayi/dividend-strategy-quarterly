"""用巨潮官方实施公告核验退市股分红事件的报告年度。

本脚本只生成独立审计产物，不修改 BaoStock 原始记录、冻结 manifest 或 V1 输入。
公告列表完整分页且串行限流；任何空结果、字段缺失或多重匹配都失败关闭。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from build_historical_universe import canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
STOCK_LIST_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
ANNOUNCEMENT_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
PDF_ROOT = "https://static.cninfo.com.cn/"
DEFAULT_INPUT = ROOT / "data" / "historical" / "delisted_dividends.json"
DEFAULT_OUTPUT = ROOT / "data" / "historical" / "delisted_dividend_report_year_verification.json"
VERIFIED_OUTPUT = ROOT / "data" / "historical" / "delisted_dividends_verified.json"
PRICE_INPUT = ROOT / "data" / "historical" / "eligible_delisted_prices.json"
DEFAULT_PROBES = (
    "600466:2019-05-08", "600565:2019-06-13",
    "600401:2012-06-08", "300104:2017-08-25",
)
SEARCH_KEYWORDS = ("实施公告",)
TITLE_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*年?\s*年度")
VERIFIER_LOGIC_VERSION = 5


class SerialLimiter:
    """保证所有外部请求串行且相邻请求满足最小间隔。"""

    def __init__(self, minimum_interval: float) -> None:
        self.minimum_interval = max(float(minimum_interval), 0.0)
        self.last_request_at: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self.last_request_at is not None:
            remaining = self.minimum_interval - (now - self.last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request_at = time.monotonic()


def build_session() -> requests.Session:
    retry = Retry(
        total=3, connect=3, read=3, backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.cninfo.com.cn/",
        "Connection": "close",
    })
    return session


def strip_title(value: str) -> str:
    return re.sub(r"<[^>]+>", "", str(value or "")).strip()


def extract_annual_report_year(title: str) -> int | None:
    """只接受年度实施公告，排除半年度、季度和说明会。"""
    clean = strip_title(title)
    if "实施" not in clean or not any(word in clean for word in (
        "权益分派", "分红派息", "利润分配", "分配派息", "分配方案", "转增股本",
    )):
        return None
    if any(word in clean for word in (
        "半年度", "中期", "季度", "说明会", "调整发行", "调整换股", "提示性公告",
        "延缓实施", "暂缓实施", "取消实施",
    )):
        return None
    match = TITLE_YEAR_RE.search(clean)
    return int(match.group(1)) if match else None


def _request_json(session: requests.Session, limiter: SerialLimiter, method: str,
                  url: str, **kwargs: Any) -> Any:
    limiter.wait()
    response = session.request(method, url, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json()


def load_stock_directory(session: requests.Session, limiter: SerialLimiter) -> dict[str, dict]:
    payload = _request_json(session, limiter, "GET", STOCK_LIST_URL)
    rows = payload.get("stockList") or []
    directory = {str(row.get("code")): row for row in rows if row.get("code") and row.get("orgId")}
    if not directory:
        raise RuntimeError("巨潮证券列表为空，拒绝继续核验")
    return directory


def query_all_announcements(
    session: requests.Session, limiter: SerialLimiter, *, code: str, org_id: str,
    keyword: str, start_date: str = "2011-01-01", end_date: str = "2026-08-27",
) -> list[dict]:
    column = "sse" if code.startswith("6") else "szse"
    page = 1
    page_size = 30
    found: dict[str, dict] = {}
    expected_total: int | None = None
    while True:
        body = {
            "pageNum": str(page), "pageSize": str(page_size), "column": column,
            "tabName": "fulltext", "plate": "", "stock": f"{code},{org_id}",
            "searchkey": keyword, "secid": "", "category": "", "trade": "",
            "seDate": f"{start_date}~{end_date}", "sortName": "", "sortType": "",
            "isHLtitle": "true",
        }
        payload = _request_json(session, limiter, "POST", ANNOUNCEMENT_URL, data=body)
        total = int(payload.get("totalAnnouncement") or 0)
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError(f"{code} {keyword} 分页期间公告总数发生变化")
        rows = payload.get("announcements") or []
        for row in rows:
            key = str(row.get("adjunctUrl") or row.get("announcementId") or "")
            if not key:
                raise RuntimeError(f"{code} {keyword} 公告缺少唯一标识")
            found[key] = row
        if not payload.get("hasMore"):
            break
        if not rows:
            raise RuntimeError(f"{code} {keyword} 声明还有下一页但当前页为空")
        page += 1
        if page > 100:
            raise RuntimeError(f"{code} {keyword} 分页超过安全上限")
    if len(found) != expected_total:
        raise RuntimeError(
            f"{code} {keyword} 公告分页不完整：期望 {expected_total}，实际 {len(found)}"
        )
    return list(found.values())


def _date_forms(value: str) -> tuple[str, ...]:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return (
        value, f"{parsed.year}/{parsed.month}/{parsed.day}",
        f"{parsed.year}年{parsed.month}月{parsed.day}日",
    )


def _first_number(text: str, patterns: tuple[str, ...], divisor: float = 1.0) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1)) / divisor
    return None


def extract_pdf_fields(pdf_bytes: bytes, expected_ex_date: str | None = None) -> dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = re.sub(r"\s+", "", "".join(page.extract_text() or "" for page in reader.pages))
    if not text:
        raise ValueError("官方 PDF 无可提取文本")
    raw_dates = re.findall(
        r"(?:19|20)\d{2}(?:"
        r"[-/](?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])(?=(?:19|20)\d{2}|\D|$)"
        r"|年(?:0?[1-9]|1[0-2])月(?:0?[1-9]|[12]\d|3[01])日)", text
    )
    dates = []
    for raw in raw_dates:
        normalized = raw.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
        parts = normalized.split("-")
        try:
            value = f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
            datetime.strptime(value, "%Y-%m-%d")
        except (ValueError, IndexError):
            continue
        if value not in dates:
            dates.append(value)
    ex_date = None
    if expected_ex_date is not None and expected_ex_date in dates:
        ex_date = expected_ex_date
    cash = _first_number(text, (
        r"每股现金红利[（(]?扣税前[）)]?[：:]?(?:人民币)?([0-9.]+)",
        r"(?:A股)?每股现金红利([0-9.]+)",
    ))
    if cash is None:
        cash = _first_number(text, (
            r"每10股派(?:送)?(?:发)?(?:现金股利)?(?:人民币)?([0-9.]+)元",
            r"每10股送(?:红股)?[0-9.]+股[，,]?派([0-9.]+)元(?:人民币)?现金",
            r"每10股派(?:发)?现金股利([0-9.]+)元",
            r"每10股派(?:发)?人民币([0-9.]+)元现金",
            r"每10股派(?:发)?(?:现金红利)?([0-9.]+)元(?:人民币)?现金",
            r"每10股派(?:发)?现金红利([0-9.]+)",
            r"每10股派息([0-9.]+)",
        ), 10.0)
    stock = _first_number(text, (
        r"每股送(?:红股)?([0-9.]+)股",
        r"每10股(?:送红股|送|派送|派发红股)([0-9.]+)股",
    ))
    if stock is not None and not re.search(r"每股送", text):
        stock /= 10.0
    transfer = _first_number(text, (r"每股转增(?:股份)?([0-9.]+)股",))
    if transfer is None:
        transfer = _first_number(text, (r"每10股(?:定向)?转增(?:股本)?([0-9.]+)股",), 10.0)
    return {
        "ex_date": ex_date,
        "all_dates": dates,
        "cash_per_share_before_tax": 0.0 if cash is None else cash,
        "stock_dividend_per_share": 0.0 if stock is None else stock,
        "reserve_to_stock_per_share": 0.0 if transfer is None else transfer,
        "page_count": len(reader.pages),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def fields_match(event: dict[str, Any], official: dict[str, Any], tolerance: float = 0.005001) -> bool:
    official_dates = official.get("all_dates") or [official.get("ex_date")]
    if event.get("ex_date") not in official_dates:
        return False
    for key in (
        "cash_per_share_before_tax", "stock_dividend_per_share",
        "reserve_to_stock_per_share",
    ):
        if abs(float(event.get(key) or 0) - float(official.get(key) or 0)) > tolerance:
            return False
    return True


def positive_events(stock: dict[str, Any]) -> list[dict[str, Any]]:
    keys = (
        "cash_per_share_before_tax", "stock_dividend_per_share",
        "reserve_to_stock_per_share",
    )
    return [row for row in stock.get("records") or []
            if any(float(row.get(key) or 0) > 0 for key in keys)]


def parse_official_announcements(session: requests.Session, limiter: SerialLimiter,
                                 announcements: list[dict]) -> list[dict[str, Any]]:
    official = []
    for announcement in announcements:
        title = strip_title(announcement.get("announcementTitle"))
        report_year = extract_annual_report_year(title)
        if report_year is None:
            continue
        url = PDF_ROOT + str(announcement.get("adjunctUrl") or "").lstrip("/")
        pdf_bytes = _get_pdf(session, limiter, url)
        official.append({
            "report_year": report_year,
            "title": title,
            "announcement_time": datetime.fromtimestamp(
                int(announcement["announcementTime"]) / 1000
            ).strftime("%Y-%m-%d"),
            "pdf_url": url,
            "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "official_fields": extract_pdf_fields(pdf_bytes),
        })
    return official


def reconcile_stock(stock: dict[str, Any], official: list[dict[str, Any]]) -> dict[str, Any]:
    """对 Bao 与官方公告做双向一一匹配，不允许任一侧有遗漏。"""
    bao = positive_events(stock)
    matches: list[tuple[int, int]] = []
    for bao_index, event in enumerate(bao):
        for official_index, document in enumerate(official):
            if fields_match(event, document["official_fields"]):
                matches.append((bao_index, official_index))
    bao_counts = {index: 0 for index in range(len(bao))}
    official_counts = {index: 0 for index in range(len(official))}
    for bao_index, official_index in matches:
        bao_counts[bao_index] += 1
        official_counts[official_index] += 1
    unmatched_bao = [bao[index] for index, count in bao_counts.items() if count == 0]
    ambiguous_bao = [bao[index] for index, count in bao_counts.items() if count > 1]
    unmatched_official = [official[index] for index, count in official_counts.items() if count == 0]
    ambiguous_official = [official[index] for index, count in official_counts.items() if count > 1]
    records = []
    for bao_index, event in enumerate(bao):
        unique = [official_index for matched_bao, official_index in matches
                  if matched_bao == bao_index and bao_counts[bao_index] == 1
                  and official_counts[official_index] == 1]
        if len(unique) != 1:
            continue
        document = official[unique[0]]
        records.append({
            **event,
            "baostock_report_year": int(event["report_year"]),
            "report_year": document["report_year"],
            "report_year_changed": int(event["report_year"]) != document["report_year"],
            "official_evidence": document,
            "verification_status": "verified_unique_match",
        })
    if unmatched_bao or ambiguous_bao or unmatched_official or ambiguous_official:
        return {
            "code": stock["code"], "name": stock.get("name"), "status": "failed_closed",
            "baostock_positive_event_count": len(bao),
            "official_implementation_count": len(official),
            "unmatched_baostock_events": unmatched_bao,
            "ambiguous_baostock_events": ambiguous_bao,
            "unmatched_official_announcements": unmatched_official,
            "ambiguous_official_announcements": ambiguous_official,
            "verified_records": records,
        }
    return {
        "code": stock["code"], "name": stock.get("name"), "status": "verified",
        "baostock_positive_event_count": len(bao),
        "official_implementation_count": len(official),
        "verified_records": records,
        "unmatched_baostock_events": [], "ambiguous_baostock_events": [],
        "unmatched_official_announcements": [], "ambiguous_official_announcements": [],
    }


def _get_pdf(session: requests.Session, limiter: SerialLimiter, url: str) -> bytes:
    limiter.wait()
    response = session.get(url, timeout=30)
    response.raise_for_status()
    if not response.content.startswith(b"%PDF"):
        raise ValueError("巨潮附件不是 PDF")
    return response.content


def verify_event(session: requests.Session, limiter: SerialLimiter, stock: dict,
                 event: dict[str, Any], announcements: list[dict]) -> dict[str, Any]:
    candidates = []
    for announcement in announcements:
        title = strip_title(announcement.get("announcementTitle"))
        report_year = extract_annual_report_year(title)
        if report_year is None:
            continue
        relative = str(announcement.get("adjunctUrl") or "")
        url = PDF_ROOT + relative.lstrip("/")
        pdf_bytes = _get_pdf(session, limiter, url)
        fields = extract_pdf_fields(pdf_bytes, event["ex_date"])
        if fields_match(event, fields):
            candidates.append({
                "report_year": report_year,
                "title": title,
                "announcement_time": datetime.fromtimestamp(
                    int(announcement["announcementTime"]) / 1000
                ).strftime("%Y-%m-%d"),
                "pdf_url": url,
                "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                "official_fields": fields,
            })
    if len(candidates) != 1:
        raise RuntimeError(
            f"{stock['code']} {event['ex_date']} 官方匹配数为 {len(candidates)}，要求唯一匹配"
        )
    evidence = candidates[0]
    return {
        "code": stock["code"], "name": stock.get("name"),
        "ex_date": event["ex_date"],
        "baostock_report_year": int(event["report_year"]),
        "verified_report_year": evidence["report_year"],
        "report_year_changed": int(event["report_year"]) != evidence["report_year"],
        "baostock_fields": {
            key: event.get(key) for key in (
                "cash_per_share_before_tax", "stock_dividend_per_share",
                "reserve_to_stock_per_share",
            )
        },
        "evidence": evidence,
        "status": "verified_unique_match",
    }


def parse_probe(value: str) -> tuple[str, str]:
    try:
        code, ex_date = value.split(":", 1)
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError
        datetime.strptime(ex_date, "%Y-%m-%d")
        return code, ex_date
    except ValueError as exc:
        raise argparse.ArgumentTypeError("事件必须为 6位代码:YYYY-MM-DD") from exc


def collect_stock_announcements(session: requests.Session, limiter: SerialLimiter,
                                code: str, org_id: str,
                                end_date: str = "2026-08-27") -> list[dict]:
    merged: dict[str, dict] = {}
    for keyword in SEARCH_KEYWORDS:
        rows = query_all_announcements(
            session, limiter, code=code, org_id=org_id, keyword=keyword,
            start_date="2012-01-01", end_date=end_date,
        )
        for row in rows:
            key = str(row.get("adjunctUrl") or row.get("announcementId") or "")
            merged[key] = row
    return [row for row in merged.values()
            if extract_annual_report_year(row.get("announcementTitle")) is not None]


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def build_full_artifact(source: dict[str, Any], input_path: Path,
                        by_code: dict[str, dict], minimum_interval: float,
                        candidate_codes: set[str] | None = None,
                        price_path: Path | None = None) -> dict[str, Any]:
    targets = source.get("stocks") or []
    candidate_codes = candidate_codes if candidate_codes is not None else {
        str(stock["code"]) for stock in targets
    }
    rows = [by_code[stock["code"]] for stock in targets if stock["code"] in by_code]
    candidate_rows = [row for row in rows if row["code"] in candidate_codes]
    all_verified = (
        len(rows) == len(targets)
        and len(candidate_rows) == len(candidate_codes)
        and all(row.get("status") == "verified" for row in candidate_rows)
    )
    manual_data_gate_complete = (
        len(rows) == len(targets)
        and len(candidate_rows) == len(candidate_codes)
    )
    data_quality_eligible_codes = sorted(
        row["code"] for row in candidate_rows if row.get("status") == "verified"
    )
    try:
        portable_input = str(input_path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        portable_input = str(input_path)
    return {
        "schema_version": 1,
        "verifier_logic_version": VERIFIER_LOGIC_VERSION,
        "kind": "delisted_dividends_officially_verified",
        "source": {
            "baostock_path": portable_input,
            "baostock_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
            "stock_directory_url": STOCK_LIST_URL,
            "announcement_query_url": ANNOUNCEMENT_URL,
            "official_pdf_root": PDF_ROOT,
            "keywords": list(SEARCH_KEYWORDS),
            "minimum_request_interval_seconds": minimum_interval,
            "price_path": (
                str(price_path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
                if price_path is not None else None
            ),
            "price_sha256": hashlib.sha256(price_path.read_bytes()).hexdigest() if price_path else None,
        },
        "target_stock_count": len(targets),
        "candidate_stock_count": len(candidate_codes),
        "filtered_non_candidate_count": len(targets) - len(candidate_codes),
        "processed_candidate_count": len(candidate_rows),
        "processed_stock_count": len(rows),
        "verified_stock_count": sum(row.get("status") == "verified" for row in candidate_rows),
        "failed_stock_count": sum(row.get("status") != "verified" for row in candidate_rows),
        "baostock_positive_event_count": sum(
            len(positive_events(stock)) for stock in targets if stock["code"] in candidate_codes
        ),
        "verified_event_count": sum(len(row.get("verified_records") or []) for row in candidate_rows),
        "provider_complete": len(rows) == len(targets),
        "independently_verified": all_verified,
        "status": "verified" if all_verified else "incomplete",
        "decision_path_resolved": manual_data_gate_complete,
        "manual_data_gate_complete": manual_data_gate_complete,
        "manual_data_gate_status": (
            "complete_with_exclusions" if manual_data_gate_complete else "incomplete"
        ),
        "data_quality_eligible_count": len(data_quality_eligible_codes),
        "data_quality_eligible_codes": data_quality_eligible_codes,
        "data_quality_eligible_codes_sha256": canonical_sha256(
            data_quality_eligible_codes
        ),
        "filtered_unverifiable_count": sum(
            row.get("status") != "verified" for row in candidate_rows
        ),
        "stocks": rows,
        "limitations": [
            "仅纳入现金、送股或转增任一非零的 BaoStock 事件。",
            "潜在候选仅由 BaoStock 历史连续正分红至少 3 年且存在有效可交易价格确定，不使用回测收益。",
            "每个 BaoStock 事件与每份官方年度实施公告必须双向一一匹配。",
            "任一股票存在空结果、缺失、多余或歧义时，全局 independently_verified 为 false。",
            "人工数据门禁会排除未形成整股双向一一匹配的候选；该过滤不使用回测收益。",
            "complete_with_exclusions 只表示所有候选均已处理，不等于全量独立核验通过。",
            "本产物独立保存，不修改冻结 V1 输入。",
        ],
    }


def run_full(input_path: Path, output_path: Path, minimum_interval: float,
             codes: set[str] | None = None,
             price_path: Path = PRICE_INPUT) -> dict[str, Any]:
    source = json.loads(input_path.read_text(encoding="utf-8"))
    targets = source.get("stocks") or []
    expected_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
    price_source = json.loads(price_path.read_text(encoding="utf-8"))
    price_codes = {
        str(row["code"]) for row in price_source.get("stocks") or []
        if row.get("provider_complete") and int(row.get("row_count") or 0) > 0
    }
    candidate_codes = {
        str(stock["code"]) for stock in targets
        if int(stock.get("max_consecutive_positive_dividend_years") or 0) >= 3
        and str(stock["code"]) in price_codes
    }
    price_hash = hashlib.sha256(price_path.read_bytes()).hexdigest()
    existing: dict[str, Any] = {}
    if output_path.exists():
        loaded = json.loads(output_path.read_text(encoding="utf-8"))
        if (
            (loaded.get("source") or {}).get("baostock_sha256") == expected_hash
            and (loaded.get("source") or {}).get("price_sha256") == price_hash
            and loaded.get("verifier_logic_version") == VERIFIER_LOGIC_VERSION
        ):
            existing = {row["code"]: row for row in loaded.get("stocks") or []
                        if row.get("status") in (
                            "verified", "filtered_non_candidate", "failed_closed"
                        )}
    if len(existing) == len(targets) and all(
        str(stock["code"]) in existing for stock in targets
    ):
        artifact = build_full_artifact(
            source, input_path, existing, minimum_interval, candidate_codes, price_path
        )
        write_json_atomic(output_path, artifact)
        return artifact
    session = build_session()
    limiter = SerialLimiter(minimum_interval)
    directory = load_stock_directory(session, limiter)
    by_code = dict(existing)
    for index, stock in enumerate(targets, 1):
        code = str(stock["code"])
        if codes is not None and code not in codes:
            continue
        if code in existing:
            print(f"核验 {index}/{len(targets)} {code}：断点已通过，跳过")
            continue
        if code not in candidate_codes:
            row = {
                "code": code, "name": stock.get("name"),
                "status": "filtered_non_candidate",
                "filter_reason": (
                    "BaoStock 历史连续正分红不足 3 年，或缺少有效可交易价格；"
                    "该过滤不使用回测收益。"
                ),
                "max_consecutive_positive_dividend_years": int(
                    stock.get("max_consecutive_positive_dividend_years") or 0
                ),
                "has_valid_tradeable_prices": code in price_codes,
                "verified_records": [],
            }
            by_code[code] = row
            artifact = build_full_artifact(
                source, input_path, by_code, minimum_interval, candidate_codes, price_path
            )
            write_json_atomic(output_path, artifact)
            print(f"核验 {index}/{len(targets)} {code}：filtered_non_candidate")
            continue
        try:
            directory_row = directory.get(code)
            if not directory_row:
                raise RuntimeError("巨潮证券列表缺少 code/orgId")
            announcements = collect_stock_announcements(
                session, limiter, code, str(directory_row["orgId"]),
                end_date=str(stock.get("delist_date") or "2026-08-27")[:10],
            )
            official = parse_official_announcements(session, limiter, announcements)
            row = reconcile_stock(stock, official)
            if row["status"] != "verified":
                row["error"] = "官方公告与 BaoStock 事件未形成双向一一匹配"
        except Exception as exc:
            row = {
                "code": code, "name": stock.get("name"), "status": "failed_closed",
                "baostock_positive_event_count": len(positive_events(stock)),
                "official_implementation_count": None, "verified_records": [],
                "error": str(exc),
            }
        by_code[code] = row
        artifact = build_full_artifact(
            source, input_path, by_code, minimum_interval, candidate_codes, price_path
        )
        write_json_atomic(output_path, artifact)
        print(
            f"核验 {index}/{len(targets)} {code}：{row['status']}，"
            f"Bao {row.get('baostock_positive_event_count')} / 官方 {row.get('official_implementation_count')}"
        )
    artifact = build_full_artifact(
        source, input_path, by_code, minimum_interval, candidate_codes, price_path
    )
    write_json_atomic(output_path, artifact)
    return artifact


def run(probes: list[tuple[str, str]], input_path: Path, minimum_interval: float) -> dict:
    source = json.loads(input_path.read_text(encoding="utf-8"))
    stocks = {str(item["code"]): item for item in source.get("stocks") or []}
    session = build_session()
    limiter = SerialLimiter(minimum_interval)
    directory = load_stock_directory(session, limiter)
    results = []
    announcement_cache: dict[str, list[dict]] = {}
    for code, ex_date in probes:
        if code not in stocks:
            raise RuntimeError(f"BaoStock 产物中没有 {code}")
        if code not in directory:
            raise RuntimeError(f"巨潮证券列表中没有 {code} 或 orgId")
        events = [row for row in stocks[code].get("records") or [] if row.get("ex_date") == ex_date]
        if len(events) != 1:
            raise RuntimeError(f"{code} {ex_date} BaoStock 事件数为 {len(events)}，要求唯一")
        if code not in announcement_cache:
            candidates = collect_stock_announcements(
                session, limiter, code, str(directory[code]["orgId"])
            )
            if not candidates:
                raise RuntimeError(f"{code} 未找到任何年度实施公告，拒绝把空结果当作无记录")
            announcement_cache[code] = candidates
        results.append(verify_event(
            session, limiter, stocks[code], events[0], announcement_cache[code]
        ))
    return {
        "schema_version": 1,
        "kind": "delisted_dividend_report_year_verification",
        "source": {
            "stock_directory_url": STOCK_LIST_URL,
            "announcement_query_url": ANNOUNCEMENT_URL,
            "official_pdf_root": PDF_ROOT,
            "keywords": list(SEARCH_KEYWORDS),
            "minimum_request_interval_seconds": minimum_interval,
        },
        "input": {
            "path": str(input_path.relative_to(ROOT)).replace("\\", "/"),
            "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        },
        "scope": {
            "requested_event_count": len(probes),
            "verified_event_count": len(results),
            "full_stock_history_verified": False,
        },
        "records": results,
        "status": "verified" if len(results) == len(probes) else "failed",
        "limitations": [
            "本产物只核验显式请求的事件，不证明整只股票分红历史完整。",
            "标题提供报告年度，PDF 的除权日和分配字段用于与 BaoStock 事件唯一匹配。",
            "任何空结果、字段不一致或多匹配均直接失败，不自动执行年份减一。",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="核验退市股分红事件的官方报告年度")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--event", action="append", type=parse_probe,
                        help="代码:除权日；可重复传入。未指定时运行四股探针")
    parser.add_argument("--full", action="store_true", help="全量双向核验并按股票断点保存")
    parser.add_argument("--code", action="append", help="全量模式仅运行指定代码，供小范围复核")
    parser.add_argument("--min-interval", type=float, default=0.6)
    args = parser.parse_args()
    if args.full:
        output = args.output if args.output != DEFAULT_OUTPUT else VERIFIED_OUTPUT
        payload = run_full(
            args.input, output, args.min_interval,
            set(args.code) if args.code else None,
        )
        print(
            f"已写入 {output}：处理 {payload['processed_stock_count']}/"
            f"{payload['target_stock_count']}，独立核验={payload['independently_verified']}"
        )
        return
    probes = args.event or [parse_probe(value) for value in DEFAULT_PROBES]
    payload = run(probes, args.input, args.min_interval)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {args.output}：唯一核验 {len(payload['records'])} 个事件")
    for row in payload["records"]:
        print(f"{row['code']} {row['ex_date']}：BaoStock {row['baostock_report_year']} -> 官方 {row['verified_report_year']}")


if __name__ == "__main__":
    main()
