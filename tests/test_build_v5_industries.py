import json

import build_v5_industries as builder
from build_v5_industries import _new_rows, _old_rows


def test旧版证监会表沿用最近行业大类():
    rows = _old_rows(
        "采矿业(B) 06 煤炭开采和洗选业 600121 郑州煤电\n"
        "600123 兰花科创\n07 石油和天然气开采业\n600028 中国石化"
    )
    assert rows == [
        {"code": "600121", "industry_code": "06", "industry": "06 煤炭开采和洗选业"},
        {"code": "600123", "industry_code": "06", "industry": "06 煤炭开采和洗选业"},
        {"code": "600028", "industry_code": "07", "industry": "07 石油和天然气开采业"},
    ]


def test新版协会表读取每只股票最后一个两位大类():
    text = (
        "000004*ST国华 I 信息传输、软件和信息技术\n"
        "服务业\n65 软件和信息技术服务业\n"
        "000008神州高铁 C 制造业 CG 专用、通用及交通运输设备 37 铁路设备制造业\n"
    )
    assert _new_rows(text) == [
        {"code": "000004", "industry_code": "65", "industry": "65 软件和信息技术服务业"},
        {"code": "000008", "industry_code": "37", "industry": "37 铁路设备制造业"},
    ]


def test行业快照覆盖科创板和北交所(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"records": [
        {"code": "600001"}, {"code": "688016"}, {"code": "920982"},
        {"code": "invalid"},
    ]}), encoding="utf-8")
    article = {
        "title": "2025年下半年上市公司行业分类结果",
        "article_url": "https://example.test/article",
        "authority": "中国上市公司协会",
    }
    source = {**article, "published_date": "2026-04-03",
              "pdf_url": "https://example.test/source.pdf"}
    rows = [
        {"code": code, "industry_code": "39", "industry": "39 计算机、通信和其他电子设备制造业"}
        for code in ("600001", "688016", "920982")
    ]
    monkeypatch.setattr(builder, "discover_articles", lambda _fetch: [article])
    monkeypatch.setattr(builder, "article_source", lambda _article, _fetch: source)
    monkeypatch.setattr(builder, "parse_pdf", lambda _content, _title: rows)

    result = builder.build_snapshot(manifest, "2026-08-31", fetch=lambda _url: b"pdf")

    assert result["coverage"] == {
        "required_codes": 3,
        "covered_codes": 3,
        "missing_codes": [],
        "missing_policy": "无官方分类的股票不得进入 V5 候选池",
    }
    assert {row["code"] for row in result["records"]} == {"600001", "688016", "920982"}
    assert "包含科创板与北交所" in result["market_scope"]
