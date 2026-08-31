from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ma_v22_strategy import MA_V22_ASSETS, load_inputs
from refresh_ma_v22_inputs import build_inputs, fetch_fund_dividends


def _asset_rows() -> dict[str, list[dict]]:
    return {
        asset: [{
            "date": "2026-08-31", "hfq_open": 1.0, "hfq_close": 1.1,
            "raw_open": 2.0, "raw_close": 2.1,
        }]
        for asset in MA_V22_ASSETS
    }


def test构建输入写入参考附件和内容哈希(tmp_path: Path) -> None:
    payload = build_inputs(_asset_rows(), [], "2026-08-31")
    path = tmp_path / "input.json"
    import json
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    loaded = load_inputs(path)
    assert loaded["as_of"] == "2026-08-31"
    assert len(loaded["attachments"]) == 8


def test四资产截止日不一致时拒绝冻结() -> None:
    rows = _asset_rows()
    rows["bond"][0]["date"] = "2026-08-28"
    with pytest.raises(ValueError, match="覆盖输入截止日"):
        build_inputs(rows, [], "2026-08-31")


def test无分红基金页面返回空列表而不是伪造记录() -> None:
    class Response:
        text = "<html><table class='cfxq'></table></html>"

        @staticmethod
        def raise_for_status() -> None:
            return None

    rows, url = fetch_fund_dividends("513100", "2026-08-31", get=lambda *args, **kwargs: Response())
    assert rows == []
    assert "513100" in url
