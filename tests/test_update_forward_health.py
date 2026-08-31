import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from update_forward_health import build_health


def test影子失败会显式保留且不伪装成功():
    result = build_health(
        "2026-08-31", "signal",
        {
            "v1": "success", "v2": "failure", "v3": "success",
            "v5": "failure", "ma_v22": "success",
        },
    )
    assert result["strategies"]["v1"]["status"] == "正常"
    assert result["strategies"]["v2"]["outcome"] == "failure"
    assert "未冒充更新成功" in result["strategies"]["v2"]["status"]
    assert result["strategies"]["v5"]["outcome"] == "failure"
    assert result["strategies"]["ma_v22"]["name"] == "多资产风险预算 V2.2（全球版影子）"
