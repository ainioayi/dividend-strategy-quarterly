# 放宽稳定性季度高股息策略

这是本次讨论形成的独立研究站点，不是 `flyshub/dividend-calculator` 的镜像。
首页完整保留 2016-2025 十年候选池回测、滚动窗口、仓位敏感性、10 万元模型账本和风险披露，并在每季度生成新的模型调仓信号。

公开页面：<https://ainioayi.github.io/dividend-strategy-quarterly/>

## 自动更新规则

- 运行时间：北京时间每年 1、4、7、10 月首个可用交易日收盘后。
- 选股顺序：先通过可持续性、分红连续性、支付率、现金覆盖和 PR 门槛，再按真实股息率从高到低排序。
- 采用策略：放宽短期稳定性计数（`min_persistence=0`），最多 10 只，同一一级行业最多 2 只，银行最多 2 只。
- 低买：真实股息率不低于 5%，PR 不高于 0.85。
- 高卖：股息率低于 4.5% 或 PR 高于 1.05 连续确认；明确的可持续性风险立即退出。
- 交易约束：100 股整数手，并计入佣金、印花税和过户费。
- 数据不完整时：任务失败并保留上一版网页，不会把缺失数据当成卖出信号。

## 页面与审计文件

- `site/index.html`：公开首页。
- `site/audit.json`：页面对应的完整审计数据。
- `site/archive/`：初始报告和后续季度归档。
- `data/ledgers/relaxed.json`：放宽稳定性模型账本。
- `data/ledgers/relaxed_cap20.json`：单票 20% 上限对照账本。
- `data/backtest_baseline.json`：2016-2025 固定候选池回测基线。

## 本地生成

```powershell
python -m pip install -r requirements.txt
python scripts/bootstrap_state.py
python scripts/generate_report.py
python -m http.server 8000 --directory site
```

实时季度刷新还需要检出上游数据模块：

```powershell
git clone https://github.com/flyshub/dividend-calculator.git _upstream
python scripts/refresh_snapshot.py --upstream-root _upstream/dividend-calculator --as-of 2026-10-08 --out data/snapshot_current.json
python scripts/update_portfolio.py --period 2026Q3 --as-of 2026-10-08
python scripts/generate_report.py
```

## 数据边界

历史结果是固定的当前候选池回放，存在幸存者偏差和样本选择偏差，不是历史逐季全市场样本外回测。季度模型会读取选股器当期最新候选名单，使用固定版本的上游计算代码重新核验，并将当期输入完整归档。自动页面维护的是研究用模型账本，不登录券商、不读取账户、不自动下单，也不承诺未来收益。

本项目复用了 `flyshub/dividend-calculator` 的 GPL-3.0 代码与数据口径，继续以 GPL-3.0 发布。
