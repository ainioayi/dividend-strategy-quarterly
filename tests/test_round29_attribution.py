"""验证第 29 轮收益归因结果。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MANIFEST_SHA = "24de009d9bb60c857fc89e8f7510b93583b17f9abde50350ea63a6a5830a7409"


def test_round29_attribution():
    """收益归因结果应与冻结输入和基线一致。"""
    p = DATA / "round29_attribution.json"
    assert p.exists(), "缺少 round29_attribution.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["round"] == 29
    # 基线指标应匹配冻结结果。
    assert d["baseline_metrics"]["cagr"] == 41.38
    assert d["baseline_metrics"]["ending_nav"] == 3876245.51
    # manifest 哈希应匹配冻结输入。
    assert d["manifest_records_sha256"] == MANIFEST_SHA
    # 收益分解应包含正的总盈亏、分红和资本利得。
    dec = d["decomposition"]
    assert dec["total_pl_incl_dividends"] > 0
    assert dec["dividend_income_pct_of_total_pl"] > 0
    assert dec["capital_pl_pct_of_total_pl"] > 0
    # 个股明细应与汇总持仓数量一致。
    assert len(d["per_stock_pl"]) >= 10
    assert d["win_rate"]["total_positions"] == len(d["per_stock_pl"])
    assert d["win_rate"]["winning_positions"] > 0
    # 年度收益应覆盖完整回测区间。
    assert len(d["yearly_returns"]) >= 10
    # 事件数量应与冻结回测一致。
    ec = d["event_counts"]
    assert ec["buys"] == 57
    assert ec["sells"] == 18
