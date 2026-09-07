# 月度高息动量策略

这是一个可复现、可审计的 A 股策略研究项目。模型只使用落盘数据，不连接券商、
不读取账户，也不会自动下单。

公开业绩：<https://ainioayi.github.io/dividend-strategy-quarterly/>

## 当前结论

当前主策略是冻结的高息动量 V1。数据截止 `2026-08-25`，共 128 个信号点，
信号在月末形成，默认在下一可用交易日收盘模拟成交。

| 指标 | 冻结 V1 |
| --- | ---: |
| CAGR（年化收益率） | 41.38% |
| 最大回撤 | 28.06% |
| Sharpe（收益与波动的匹配程度） | 1.217 |
| 交易次数 | 75 |
| 滚动 36 月最差 CAGR | 9.58% |
| 滚动 48 月最差 CAGR | 17.29% |

这些数字来自冻结历史样本，不代表未来收益，也不构成投资建议。人工数据质量门禁
历史池的回放 CAGR 仅为 11.29%，说明结果对股票覆盖范围敏感。当前集合仍可能
缺少退市股票和历史成分变化，存在幸存者偏差；月度最大回撤也会忽略月内波动。

完整策略规则、输入指纹和各轮结论以 [策略状态](docs/STRATEGY_STATUS.md)为准。
详细实验见 [探索日志](docs/EXPLORATION_LOG.md)。

## 前向观察

项目独立跟踪五套 10 万元模拟账户：高息动量 V1（正式）、V2/V3/V5（影子）
和多资产风险预算 V2.2（影子），并以 510300 沪深 300 ETF 作基准。历史回测
不会混入模拟账户。观察期至少 6 个月、目标 12 个月，期间冻结规则，固定比较
收益、回撤、换手和费用，不根据短期结果临时改标准。

观察规则、价格日期和数据缺口处理见 [前向观察计划](docs/FORWARD_OBSERVATION.md)。
候选池输入规则见 [候选池清单说明](docs/UNIVERSE_MANIFEST.md)。

## 本地检查

安装项目现有 Python 依赖后，在 PowerShell 中运行：

```powershell
python -m pip install -r requirements.txt
python scripts/check_project.py
```

这条命令校验冻结输入指纹、五策略合同，在系统临时目录复算并完整比较
`data/current_best.json`，离线复核公开快照，再运行相关测试、完整测试、Python 编译和 Git 差异
检查。它默认不联网，也不会覆盖正式输入、结果或账本；失败时返回非零退出码，
并打印隔离产物路径。

需要额外验证五策略信号、成交和业绩生成链路时运行：

```powershell
python scripts/check_project.py --rehearsal
```

该演练使用隔离副本和模拟未来价格，只证明程序链路，不是预测或选股信号。

## 常用入口

```powershell
# strategy 可替换为 v2、v3、v5、ma_v22
python scripts/monthly_forward.py --strategy v1 verify
python scripts/round31_holdings_sweep.py
python scripts/v5_strategy.py --input data/v5_inputs.json --dates data/rebalance_dates_monthly.json --cache-dir data/backtest_cache --output data/round32_v5_rebuild.json --initial-capital 1000000
python scripts/ma_v22_strategy.py --input data/ma_v22_inputs.json --output data/round33_ma_v22_rebuild.json --initial-capital 1000000
```

继续实验前先读 [探索日志](docs/EXPLORATION_LOG.md)。修改候选池、截止日或输入
文件前先读 [候选池清单说明](docs/UNIVERSE_MANIFEST.md)，并保持基线和挑战者
使用相同输入、价格及费用口径。

## 自动更新与部署

GitHub Actions 在工作日北京时间 18:30 至 22:30 按小时尝试更新。任务先检查
五策略合同、交易日历和当日行情；门禁失败时不发布旧数据。成功后更新前向业绩
并部署 GitHub Pages。手动触发隔离演练：

```powershell
gh workflow run monthly-forward.yml -f mode=rehearsal
```

该模式不会修改正式账本，也不会创建或关闭正式更新告警。

发布门禁、停牌核验和历史净值更正流程见 [公开数据运维](docs/FORWARD_OPERATIONS.md)。

## 主要文件

- `data/current_best.json`：冻结 V1 的完整回测结果。
- `data/v1_freeze.json`：V1 参数、输入和结果指纹。
- `data/forward/`：前向缓存、版本化输入和只追加模拟账本。
- `site/`：公开业绩页面与发布数据。
- `docs/STRATEGY_STATUS.md`：当前结果的唯一权威说明。
- `docs/EXPLORATION_LOG.md`：历轮实验、限制和取舍。

项目沿用 `flyshub/dividend-calculator` 的 GPL-3.0 代码和数据口径，并继续以
GPL-3.0 发布。
