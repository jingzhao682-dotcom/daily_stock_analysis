# 报告质量与数据边界

## 市场评分

- 市场收益维度只使用有效的股票指数行情；VIX / ^VIX 继续显示在行情表中，但不参与股指涨跌均值和正向评分。美股指数别名归一化后去重，缺失、非有限数值和零点位不视为有效行情。
- 保留原有 45% 市场宽度、35% 指数、20% 涨跌停评分权重，缺失维度的 50 分只是兼容占位，不是观测值。缺失状态继续通过 `dimensions.available` 和 `data_quality` 披露；部分数据不能给出“可进攻”信号，无有效股指时显示“数据不足”。该评分未经收益回测，不是组合仓位模型。
- 美股证据不完整时，复盘使用确定性行情观察模板，保留已取得的新闻条目，不会据此生成组合仓位、现金比例或长期配置变动建议。其他市场保留现有模型生成路径。

## 个股行情

- 实时报价的昨收来自同一报价的 `pre_close`，不借用可能经过复权的历史日线。缺少匹配昨收时，价差和涨跌幅显示 N/A。
- 展示表中的涨跌额、涨跌幅和振幅基于同列价格对计算。供应商百分比保存在 `reported_pct_chg`；若相差超过 0.02 个百分点，则标记 partial 并披露冲突，保留原始输入，不将展示收益冒充供应商的真实/总回报。应进一步核验除息、拆股、复权和时点差异。
- 邮件模板、备用文本和历史报告显示币种、数据日期、供应商行情时点、来源及口径说明。缺失的时点显示 N/A，不推测上一交易日；报告日与行情日期分别保存。
- 美股指数使用 Yahoo 日线携带的实际交易日标注复盘日期；日期未知时不推测前一个工作日，已知交易日混杂时不计算单日综合收益。仅有一根指数日线、没有昨收时返回不可用，而不是伪造 0% 涨跌。

## 建议降级

- 核心行情、日线、技术数据 stale / fallback / missing / fetch_failed / partial / estimated，或价格快照不一致时，操作动作降为 watch，统计决策为 hold。清理旧执行点位和仓位建议，保存降级原因，防止展示层依据极端评分重新生成买卖标签。
- 已知没有新闻证据时，移除模型生成的新闻贡献权重，不凭空重新分配其他权重。
- 降级说明不等于卖出信号，也不替代长期 ETF 配置政策。系统不会执行自动交易。

## 验证与回滚

`python -m pytest tests/test_p0_report_quality.py` 是离线回归入口，不运行日报、不发送邮件。完整后端检查使用 `./scripts/ci_gate.sh`。本修复不改变邮件设置、Secrets 或定时任务。

如需回滚，应整体 revert 对应 PR 的提交；不要只撤回动作护栏而保留新数据质量字段。

## English reference

VIX remains visible but is excluded from equity-return averages and scoring. Existing score weights are retained; unavailable dimensions are explicitly marked and partial signals cannot authorize a risk-on allocation. Limited US reviews use deterministic observations, not generated cash targets.

Live prices use their own prior close, never adjusted historical closes. Displayed price-pair returns retain the provider's original percentage separately; conflicts are disclosed and downgrade decisions. Missing quote times remain unknown. Core degraded inputs lead to watch/hold across decision surfaces, without execution levels or unsupported news weights. No trading, scheduling, credential or email delivery behavior is added.

## 2026-10-20 报告完整性修复

- 美股复盘以至少两项核心股票指数的交易日为基准；不足时日期为 N/A。异日 VIX 从当日指数统计排除并披露，不能用报告生成日代替行情日。
- 新增 Gold-API XAU 参考现货报价，校验 USD、XAU、正数价格及带时区的更新时间（12 小时内）；失败回退 Yahoo/Stooq，均失败明确缺失。AU0 为新浪/AKShare 的沪金连续期货收盘价，交易日期单独列出，非上海黄金交易所现货。
- 请求名单与成功结果分开：汇总报告披露请求、成功、失败数量及失败标的。Actions 成功仅代表程序完成，不能代表所有标的或行情源成功。失败不等于剔除/卖出。
- 换手率使用行情快照，缺失为 N/A，不采用模型默认 0；内置与 Jinja 报告一致。
- 无核验资金流时，量比不能支持“主力介入/抄底盘/主动性买盘”等结论。无 SEC 原始财报链接时，报表金额、ROE、营收/利润增长率及利润率一并隐藏；有链接仅代表来源引用存在，不表示逐项核对过财报。分红仍为供应商数据，不伪称发行人核验。
- 公告列表展示各条发布日期，避免将 7 日官方 RSS 窗口误写成近三日。

English: Quote-session anchoring, timestamped XAU references, explicit partial-batch coverage, provider-based missing turnover and filing provenance gates apply to all aggregate reports. These checks neither authorize nor execute trades. Revert this PR as a unit to roll back.
