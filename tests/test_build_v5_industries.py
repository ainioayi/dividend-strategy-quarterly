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
