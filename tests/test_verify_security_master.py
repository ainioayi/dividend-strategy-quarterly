import hashlib

import pytest

from verify_security_master import build_verification, validate_exception_evidence


def _master():
    return {
        "source_snapshot_date": "2026-08-27",
        "records": [
            {"code": "600001", "name": "在市", "list_date": "2000-01-01",
             "delist_date": "", "status": "listed"},
            {"code": "600002", "name": "退市", "list_date": "2000-01-01",
             "delist_date": "2020-01-02", "status": "delisted"},
            {"code": "600003", "name": "合并", "list_date": "2000-01-01",
             "delist_date": "2020-01-03", "status": "delisted"},
        ],
    }


def _official():
    return {
        "sources": {"sse": {"response_sha256": "a" * 64}},
        "current": [{"code": "600001", "name": "在市", "list_date": "2000-01-01",
                     "termination_date": "", "exchange": "SH"}],
        "delisted": [{"code": "600002", "name": "退市", "list_date": "2000-01-01",
                      "termination_date": "2020-01-03", "exchange": "SH"}],
    }


def test_master_fails_closed_for_unverified_merger_exception():
    payload = build_verification(_master(), _official(), [])
    assert payload["current_sets_match"] is True
    assert payload["official_delisted_is_subset"] is True
    assert payload["independently_verified"] is False
    assert payload["differences"]["unresolved_exceptions"] == ["600003"]
    assert payload["differences"]["invalid_termination_order"] == []
    assert payload["differences"]["delisted_date_differences"][0] == {
        "code": "600002",
        "last_trading_date": "2020-01-02",
        "official_termination_date": "2020-01-03",
    }


def test_verified_corporate_action_exception_closes_gate():
    content = b"official evidence"
    exception = {
        "code": "600003",
        "category": "merger_code_migration",
        "independently_verified": True,
        "evidence_url": "https://static.cninfo.com.cn/official.pdf",
        "evidence_sha256": hashlib.sha256(content).hexdigest(),
    }

    class Response:
        def __init__(self):
            self.content = content

        def raise_for_status(self):
            return None

    checked, codes = validate_exception_evidence([exception], lambda *args, **kwargs: Response())
    payload = build_verification(_master(), _official(), checked, codes)
    assert payload["independently_verified"] is True
    assert payload["differences"]["unresolved_exceptions"] == []


def test_untrusted_or_unfetched_exception_cannot_close_gate():
    exception = {
        "code": "600003",
        "category": "merger_code_migration",
        "independently_verified": True,
        "evidence_url": "https://example.invalid/official.pdf",
        "evidence_sha256": "b" * 64,
    }
    with pytest.raises(RuntimeError, match="域名不受信任"):
        validate_exception_evidence([exception])
    payload = build_verification(_master(), _official(), [exception])
    assert payload["independently_verified"] is False


def test_duplicate_master_code_fails_closed():
    master = _master()
    master["records"].append(dict(master["records"][0]))
    with pytest.raises(RuntimeError, match="重复代码"):
        build_verification(master, _official(), [])
