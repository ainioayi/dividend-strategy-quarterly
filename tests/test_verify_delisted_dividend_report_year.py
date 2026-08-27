from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_delisted_dividend_report_year as verify


def test_title_year_only_accepts_annual_implementation_announcements():
    assert verify.extract_annual_report_year("2018年年度权益分派实施公告") == 2018
    assert verify.extract_annual_report_year("2011年度利润分配实施公告") == 2011
    assert verify.extract_annual_report_year("2012年度分配派息实施公告") == 2012
    assert verify.extract_annual_report_year("2014年度资本公积金转增股本实施公告") == 2014
    assert verify.extract_annual_report_year("2019年度利润分配方案公告") is None
    assert verify.extract_annual_report_year("2022年半年度权益分派实施公告") is None
    assert verify.extract_annual_report_year("利润分配投资者说明会") is None


def test_fields_match_requires_ex_date_and_all_distribution_fields():
    event = {
        "ex_date": "2017-08-25", "cash_per_share_before_tax": 0.028,
        "stock_dividend_per_share": 0.0, "reserve_to_stock_per_share": 1.0,
    }
    assert verify.fields_match(event, dict(event))
    assert not verify.fields_match(event, dict(event, ex_date="2017-08-24"))
    assert not verify.fields_match(event, dict(event, reserve_to_stock_per_share=0.0))


def test_legacy_per_ten_share_cash_phrases_keep_gross_amount():
    patterns = (
        r"每10股派(?:送)?(?:发)?(?:现金股利)?(?:人民币)?([0-9.]+)元",
        r"每10股送(?:红股)?[0-9.]+股[，,]?派([0-9.]+)元(?:人民币)?现金",
    )
    assert verify._first_number("每10股派现金股利人民币0.1元", patterns, 10) == pytest.approx(0.01)
    assert verify._first_number("每10股派送0.7元（含税）现金红利", patterns, 10) == pytest.approx(0.07)
    assert verify._first_number("每10股送红股3股，派1.00元人民币现金", patterns, 10) == pytest.approx(0.1)


def test_pdf_cash_parser_divides_per_ten_share_amount(monkeypatch):
    class Page:
        def extract_text(self):
            return "除权日2025年6月1日 每10股派发现金红利3.5元"

    class Reader:
        def __init__(self, _stream):
            self.pages = [Page()]

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    fields = verify.extract_pdf_fields(b"%PDF fake", "2025-06-01")
    assert fields["cash_per_share_before_tax"] == 0.35


def test_complete_pagination_deduplicates_and_checks_total():
    class Session:
        def __init__(self):
            self.page = 0

        def request(self, method, url, timeout, **kwargs):
            self.page += 1
            payload = {
                "totalAnnouncement": 2,
                "hasMore": self.page == 1,
                "announcements": [{"adjunctUrl": f"p{self.page}.pdf"}],
            }

            class Response:
                def raise_for_status(self):
                    return None

                def json(self):
                    return payload

            return Response()

    rows = verify.query_all_announcements(
        Session(), verify.SerialLimiter(0), code="600466", org_id="gssh0600466",
        keyword="权益分派",
    )
    assert [row["adjunctUrl"] for row in rows] == ["p1.pdf", "p2.pdf"]


def test_incomplete_pagination_fails_closed():
    class Session:
        def request(self, method, url, timeout, **kwargs):
            class Response:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "totalAnnouncement": 2, "hasMore": False,
                        "announcements": [{"adjunctUrl": "only.pdf"}],
                    }
            return Response()

    with pytest.raises(RuntimeError, match="分页不完整"):
        verify.query_all_announcements(
            Session(), verify.SerialLimiter(0), code="600466", org_id="gssh0600466",
            keyword="权益分派",
        )


def test_multiple_official_matches_fail_closed(monkeypatch):
    event = {
        "report_year": 2019, "ex_date": "2019-05-08",
        "cash_per_share_before_tax": 0.26,
        "stock_dividend_per_share": 0.0, "reserve_to_stock_per_share": 0.0,
    }
    announcements = [
        {"announcementTitle": "2018年年度权益分派实施公告", "adjunctUrl": "a.pdf",
         "announcementTime": 1556409600000},
        {"announcementTitle": "2018年年度权益分派实施公告（修订）", "adjunctUrl": "b.pdf",
         "announcementTime": 1556409600000},
    ]
    monkeypatch.setattr(verify, "_get_pdf", lambda *args: b"%PDF fake")
    monkeypatch.setattr(verify, "extract_pdf_fields", lambda *args: {
        "ex_date": "2019-05-08", "cash_per_share_before_tax": 0.26,
        "stock_dividend_per_share": 0.0, "reserve_to_stock_per_share": 0.0,
    })
    with pytest.raises(RuntimeError, match="官方匹配数为 2"):
        verify.verify_event(None, verify.SerialLimiter(0), {"code": "600466"}, event, announcements)


def test_probe_parser_rejects_ambiguous_input():
    assert verify.parse_probe("600466:2019-05-08") == ("600466", "2019-05-08")
    with pytest.raises(Exception):
        verify.parse_probe("600466")


def test_real_probe_artifact_has_unique_evidence_without_claiming_full_history():
    payload = __import__("json").loads(verify.DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    assert payload["status"] == "verified"
    assert payload["scope"] == {
        "requested_event_count": 4,
        "verified_event_count": 4,
        "full_stock_history_verified": False,
    }
    assert {
        (row["code"], row["baostock_report_year"], row["verified_report_year"])
        for row in payload["records"]
    } == {
        ("600466", 2019, 2018),
        ("600565", 2019, 2018),
        ("600401", 2012, 2011),
        ("300104", 2017, 2016),
    }
    for row in payload["records"]:
        assert row["status"] == "verified_unique_match"
        assert len(row["evidence"]["pdf_sha256"]) == 64
        assert row["evidence"]["official_fields"]["ex_date"] == row["ex_date"]


def _event(ex_date="2019-05-08", cash=0.26):
    return {
        "report_year": 2019, "ex_date": ex_date,
        "cash_per_share_before_tax": cash,
        "stock_dividend_per_share": 0.0, "reserve_to_stock_per_share": 0.0,
    }


def _official(ex_date="2019-05-08", cash=0.26, year=2018, suffix="a"):
    return {
        "report_year": year, "title": f"{year}年年度权益分派实施公告",
        "announcement_time": "2019-04-28", "pdf_url": f"https://example/{suffix}.pdf",
        "pdf_sha256": suffix * 64,
        "official_fields": {
            "ex_date": None, "all_dates": [ex_date],
            "cash_per_share_before_tax": cash,
            "stock_dividend_per_share": 0.0, "reserve_to_stock_per_share": 0.0,
        },
    }


def test_reconcile_stock_requires_bidirectional_one_to_one_match():
    stock = {"code": "600466", "name": "测试", "records": [_event()]}
    result = verify.reconcile_stock(stock, [_official()])
    assert result["status"] == "verified"
    assert result["verified_records"][0]["baostock_report_year"] == 2019
    assert result["verified_records"][0]["report_year"] == 2018


def test_reconcile_stock_detects_missing_and_extra_events():
    stock = {"code": "600466", "name": "测试", "records": [_event()]}
    missing = verify.reconcile_stock(stock, [])
    assert missing["status"] == "failed_closed"
    assert len(missing["unmatched_baostock_events"]) == 1

    extra = verify.reconcile_stock(
        {"code": "600466", "name": "测试", "records": []}, [_official()]
    )
    assert extra["status"] == "failed_closed"
    assert len(extra["unmatched_official_announcements"]) == 1


def test_failed_stock_keeps_other_uniquely_verified_events():
    stock = {
        "code": "600466", "name": "测试",
        "records": [_event(), _event(ex_date="2020-07-06", cash=0.29)],
    }
    result = verify.reconcile_stock(stock, [_official()])
    assert result["status"] == "failed_closed"
    assert len(result["verified_records"]) == 1
    assert result["verified_records"][0]["ex_date"] == "2019-05-08"


def test_reconcile_stock_rejects_ambiguous_matches():
    stock = {"code": "600466", "name": "测试", "records": [_event()]}
    result = verify.reconcile_stock(stock, [_official(suffix="a"), _official(suffix="b")])
    assert result["status"] == "failed_closed"
    assert len(result["ambiguous_baostock_events"]) == 1


def test_full_artifact_only_marks_independent_verification_when_every_stock_passes(tmp_path):
    source_path = tmp_path / "source.json"
    source = {"stocks": [
        {"code": "600001", "records": [_event()]},
        {"code": "600002", "records": []},
    ]}
    source_path.write_text(__import__("json").dumps(source), encoding="utf-8")
    verified = {
        "600001": {"code": "600001", "status": "verified", "verified_records": [{}]},
        "600002": {"code": "600002", "status": "verified", "verified_records": []},
    }
    complete = verify.build_full_artifact(source, source_path, verified, 0.6)
    assert complete["independently_verified"] is True
    incomplete = verify.build_full_artifact(source, source_path, {"600001": verified["600001"]}, 0.6)
    assert incomplete["independently_verified"] is False
    assert incomplete["status"] == "incomplete"


def test_manual_gate_resolves_failed_candidate_as_explicit_exclusion(tmp_path):
    source_path = tmp_path / "source.json"
    source = {"stocks": [{"code": "600001", "records": [_event()]}]}
    source_path.write_text(__import__("json").dumps(source), encoding="utf-8")
    artifact = verify.build_full_artifact(
        source,
        source_path,
        {"600001": {
            "code": "600001", "status": "failed_closed",
            "verified_records": [], "error": "官方事件不一致",
        }},
        0.6,
        candidate_codes={"600001"},
    )
    assert artifact["manual_data_gate_complete"] is True
    assert artifact["manual_data_gate_status"] == "complete_with_exclusions"
    assert artifact["data_quality_eligible_codes"] == []
    assert artifact["filtered_unverifiable_count"] == 1
    assert artifact["independently_verified"] is False


def test_filtered_non_candidates_do_not_block_candidate_verification(tmp_path):
    source_path = tmp_path / "source.json"
    source = {"stocks": [
        {"code": "600001", "records": [_event()]},
        {"code": "600002", "records": []},
    ]}
    source_path.write_text(__import__("json").dumps(source), encoding="utf-8")
    rows = {
        "600001": {"code": "600001", "status": "verified", "verified_records": [{}]},
        "600002": {"code": "600002", "status": "filtered_non_candidate", "verified_records": []},
    }
    artifact = verify.build_full_artifact(
        source, source_path, rows, 0.6, candidate_codes={"600001"}
    )
    assert artifact["candidate_stock_count"] == 1
    assert artifact["filtered_non_candidate_count"] == 1
    assert artifact["failed_stock_count"] == 0
    assert artifact["independently_verified"] is True
