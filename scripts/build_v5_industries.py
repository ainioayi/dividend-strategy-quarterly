"""从证监会/中国上市公司协会官方 PDF 冻结 V5 历史行业分类。"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

from pypdf import PdfReader

from build_historical_universe import canonical_sha256, write_json_atomic


USER_AGENT = "Mozilla/5.0 V5 industry snapshot builder"
CSRC_LISTS = [
    "https://www.csrc.gov.cn/csrc/c100103/common_list.shtml",
    "https://www.csrc.gov.cn/csrc/c100103/common_list_1.shtml",
    "https://www.csrc.gov.cn/csrc/c100103/common_list_2.shtml",
]
CAPCO_LISTS = [
    "https://www.capco.org.cn/xhgg/hyfl/hyfljg/index.html",
    *[
        f"https://www.capco.org.cn/xhgg/hyfl/hyfljg/index_{page}.html"
        for page in range(1, 5)
    ],
]
CSRC_2015_Q4 = "https://www.csrc.gov.cn/csrc/c100103/c1452012/content.shtml"


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _clean_html(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def discover_articles(fetch=_fetch) -> list[dict[str, str]]:
    """发现所需官方分类文章；旧期取证监会，2023 年后取协会。"""
    found: dict[str, dict[str, str]] = {}
    for source, pages in (("csrc", CSRC_LISTS), ("capco", CAPCO_LISTS)):
        for page_url in pages:
            body = fetch(page_url).decode("utf-8", "ignore")
            for href, raw_title in re.findall(
                r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                body,
                flags=re.I | re.S,
            ):
                title = _clean_html(raw_title).replace(".", "")
                match = re.search(r"(20\d{2})年.*上市公司行业分类结果", title)
                if not match:
                    continue
                year = int(match.group(1))
                if (source == "csrc" and 2015 <= year <= 2021) or (
                    source == "capco" and year >= 2023
                ):
                    found[title] = {
                        "title": title,
                        "article_url": urljoin(page_url, href),
                        "authority": "中国证监会" if source == "csrc" else "中国上市公司协会",
                    }
    found.setdefault(
        "2015年4季度上市公司行业分类结果",
        {
            "title": "2015年4季度上市公司行业分类结果",
            "article_url": CSRC_2015_Q4,
            "authority": "中国证监会",
        },
    )
    return sorted(found.values(), key=lambda row: row["title"])


def article_source(article: dict[str, str], fetch=_fetch) -> dict[str, str]:
    body = fetch(article["article_url"]).decode("utf-8", "ignore")
    date_match = re.search(r"(?:日期：|content=['\"][^'\"]*,\s*)(20\d{2}-\d{2}-\d{2})", body)
    if not date_match:
        date_match = re.search(r"/(20\d{2})(\d{2})(\d{2})/", article["article_url"])
        if not date_match:
            raise ValueError(f"文章缺少可审计发布日期: {article['article_url']}")
        published_date = "-".join(date_match.groups())
    else:
        published_date = date_match.group(1)
    pdfs = [item.strip() for item in re.findall(
        r'href=["\']([^"\']+\.pdf)\s*["\']', body, flags=re.I
    )]
    if not pdfs:
        raise ValueError(f"文章缺少 PDF: {article['article_url']}")
    # 新版文章同时给代码排序与行业排序，优先代码排序。
    href = next((item for item in pdfs if "daima" in item.lower()), pdfs[0])
    return {**article, "published_date": published_date,
            "pdf_url": urljoin(article["article_url"], href)}


def _old_rows(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    industry_code = industry_name = ""
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        detailed = re.search(r"(?<!\d)(\d{2})\s+(.+?)\s+(\d{6})(?!\d)", line)
        if detailed:
            industry_code, industry_name, code = detailed.groups()
        else:
            heading = re.match(r"(\d{2})\s+(.+)$", line)
            if heading:
                industry_code, industry_name = heading.groups()
                continue
            stock = re.match(r"(\d{6})(?!\d)", line)
            if not stock or not industry_code:
                continue
            code = stock.group(1)
        rows.append({"code": code, "industry_code": industry_code,
                     "industry": f"{industry_code} {industry_name}"})
    return rows


def _new_rows(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    blocks = re.split(r"(?m)(?=^\d{6})", text)
    for block in blocks:
        stock = re.match(r"(\d{6})(?!\d)", block)
        if not stock:
            continue
        matches = re.findall(r"(?:^|\s)(\d{2})\s+([^\n]+)", block)
        if not matches:
            continue
        industry_code, industry_name = matches[-1]
        industry_name = re.sub(r"\s+", " ", industry_name).strip()
        rows.append({"code": stock.group(1), "industry_code": industry_code,
                     "industry": f"{industry_code} {industry_name}"})
    return rows


def parse_pdf(content: bytes, title: str) -> list[dict[str, str]]:
    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)
    if not text.strip():
        raise ValueError(f"官方行业 PDF 无法提取文字: {title}")
    year_match = re.search(r"(20\d{2})年", title)
    if not year_match:
        raise ValueError(f"无法识别分类年度: {title}")
    rows = _new_rows(text) if int(year_match.group(1)) >= 2023 else _old_rows(text)
    if len(rows) < 1000:
        raise ValueError(f"行业 PDF 解析记录异常少: {title}, {len(rows)}")
    return rows


def build_snapshot(manifest_path: Path, as_of: str, fetch=_fetch) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    codes = {
        str(row["code"])
        for row in manifest.get("records", [])
        if re.fullmatch(r"\d{6}", str(row.get("code", "")))
    }
    if not codes:
        raise ValueError("manifest 没有 V5 股票代码")
    records, sources = [], []
    for article in discover_articles(fetch):
        source = article_source(article, fetch)
        if source["published_date"] > as_of:
            continue
        pdf = fetch(source["pdf_url"])
        sha256 = hashlib.sha256(pdf).hexdigest()
        parsed = parse_pdf(pdf, source["title"])
        selected = [row for row in parsed if row["code"] in codes]
        records.extend({
            **row,
            "published_date": source["published_date"],
            "classification": source["title"],
            "authority": source["authority"],
            "article_url": source["article_url"],
            "source_url": source["pdf_url"],
            "source_sha256": sha256,
        } for row in selected)
        sources.append({**source, "sha256": sha256, "parsed_rows": len(parsed),
                        "selected_rows": len(selected)})
    records.sort(key=lambda row: (row["published_date"], row["code"]))
    covered = {row["code"] for row in records}
    missing = sorted(codes - covered)
    payload = {
        "schema_version": 1,
        "as_of": as_of,
        "market_scope": "冻结 manifest 内全部 A 股，包含科创板与北交所",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "records": records,
        "sources": sources,
        "coverage": {
            "required_codes": len(codes),
            "covered_codes": len(covered),
            "missing_codes": missing,
            "missing_policy": "无官方分类的股票不得进入 V5 候选池",
        },
    }
    payload["content_sha256"] = canonical_sha256(payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="冻结 V5 官方历史行业分类")
    parser.add_argument("--manifest", type=Path, default=Path("data/universe_manifest.json"))
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", type=Path, default=Path("data/v5_industries.json"))
    args = parser.parse_args()
    write_json_atomic(args.output, build_snapshot(args.manifest, args.as_of))


if __name__ == "__main__":
    main()
