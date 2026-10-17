# IBKR 持仓只读导入（第一阶段）

本阶段复用现有持仓上下文、分析任务队列和报告模板，将本机 IBKR Activity Flex XML 接入股票分析。默认运行方式、STOCK_LIST、Gemini、邮件和 GitHub cron 均不变。没有 IBKR 登录、交易连接或订单接口，也不会把快照伪造成历史买入事件。

## 本阶段能做什么

- 从一个明确选择的账户、一个报表期末读取持仓及现金；保留报表日期、导入时间、币种、FX 口径、文件指纹与过期状态。
- 预览只读快照，不写账户账本或数据库，不触发分析、不发邮件。重复预览不会重复增加持仓。
- 只分析已验证为美国交易所上市、USD 报价、乘数为 1 的 STK 持仓（包括此类 ETF）；其他资产仍计入报表持仓总值并明确标记不支持个股分析。不会将期权当成股票。
- 将每只股票自己的数量、成本、占比及日期传给现有分析流程，在报告中加入确定性的持仓观察说明。导入模式统一 watch/hold，清空执行点位及仓位建议，避免把短期读数变为长期配置指令。

尚不包含自动 Flex 下载、桌面上传界面、新桌面安装包、目标配置/再平衡策略、ETF 穿透、历史组合回撤或 GPT API 接入。真实账户和报表格式仍需用户本机验收。GitHub 托管任务无法直接访问你电脑上的报表文件，本 PR 不上传持仓，也不更改云端任务。

## 准备 IBKR 报表

在 IBKR Client Portal 的 Reports / Flex Queries 创建 **Activity Flex Query**，输出选 XML。需要以下段与字段（不是 Trade Confirmation）：

| 段 | 字段 / 要求 |
|---|---|
| FlexStatement | accountId、toDate；一个账户一个期间 |
| AccountInformation | currency（账户报告基准币种） |
| OpenPositions | SUMMARY 明细；不要同时选 LOT 或资产小计。字段：conid、symbol、assetCategory、listingExchange、currency、fxRateToBase、position、markPrice、positionValue、costBasisMoney、multiplier；有 reportDate 时必须匹配 toDate |
| CashReport / CashReportCurrency | endingCash；一个 BASE_SUMMARY 汇总。若仅有单一基准币种明细也可接受；多币种不能将明细和汇总相加。字段 toDate 如存在须匹配报表期末 |

空账户也必须包含明确的 OpenPositions 空段和现金段；缺段不代表零持仓。多账户文件必须显式选择，不自动合并账户。报表包含原始个人信息，请保存在本机私有目录，不放入 Git 仓库、PR、日志或共享预览。

参考：[IBKR Flex Query 配置](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration/create-a-flex-query)、[现金报表口径](https://www.ibkrguides.com/reportingreference/reportguide/cashreport.htm)。

## 本机使用

在已有项目运行环境中：

```bash
python main.py --portfolio ibkr --ibkr-flex-file /path/to/activity-flex.xml --no-notify
```

多账户时增加 `--ibkr-account YOUR_ACCOUNT_ID`。这会覆盖本轮的 `--stocks` / STOCK_LIST，仅分析报表内支持的持仓；不会修改持久化股票列表。缺失/损坏文件直接报错，绝不回退到默认股票列表并假装是账户持仓。上述命令会调用现有分析模型，但 `--no-notify` 禁用本轮通知；没有发送任何 IBKR 交易。

长驻本地调度可使用相同参数配合 `--schedule`，每轮重新读取文件；请自行更新导出的报表。本功能不自动刷新报表。报表距当前 UTC 日期超过 4 个自然日时标记 stale（兼容一般周末）；不能用导入时间冒充行情日期。

CLI 多账户选择器只用于本机选取，不进入归一化快照、模型输入或报告；不记录报表原文。模型只收到当前标的的标准化持仓上下文，报告会保存该标的数量、成本和占比观察；启用外部 LLM 意味着这些标准化持仓数据会发送到所配置的模型服务商。

## Web / Desktop 后端接口

沿用现有服务认证边界，均为 multipart/form-data。使用本机已启动的 API 服务：

- `POST /api/v1/portfolio/imports/ibkr/flex/parse`：上传 `file`，可选 `account`。返回 `IBKRFlexSnapshotResponse`；不持久化上传文件、不写账本、不启动任务。
- `POST /api/v1/portfolio/imports/ibkr/flex/positions/{symbol}/analysis`：同样上传文件，可选 `account`；只有确实存在且支持分析的持仓才能入队。复用任务去重与阶段处理，返回既有 `TaskAccepted`，通知固定关闭。解析、持仓核对失败不会入队。

本次只新增后端接口；现有手工账本、CSV 成交导入、持仓页面和 Futu 行为保持原有契约。后续桌面上传页应调用这些接口，避免另写一套计算逻辑。

## 计算及边界

`market_value_base = positionValue × fxRateToBase`。严格检查数量 × markPrice × multiplier 与 positionValue（容差为 0.02 原币或绝对市值的 0.01%，取较大值）；拒绝非有限数值、错误币种、日期混用、重复合约、LOT/汇总重复与异常账户归属。支持空仓、空头和负现金；净额非正时占比为 null。

`reported_assets_total = endingCash + Σ持仓市值（报告基准币种）`。这是**报表现金及所列持仓净额**，不是完整账户 NAV（可能不含应计利息、税款及其他科目）。持仓占比的分母始终使用此净额，允许杠杆情况下超过 100% 或空头负占比；gross_exposure 另列多空绝对敞口，不能与净额混淆。

成本及未实现损益使用报告成本和当期 FX，不声称衡量历史汇兑损益或总回报；缺少长期目标配置时不产生再平衡建议。本阶段 data_quality 固定 partial，limitations 显式披露这些限制。

XML 仅接受 UTF-8、最大 5 MiB；禁用 DTD/实体，不执行报表内的内容或跳转。外部账号、持有人名称、自由文本和合约 ID 不进入响应/模型上下文。没有自动采集凭证或更改 GitHub Secrets。

## 验证与回滚

离线回归入口：`python -m pytest tests/test_ibkr_flex.py tests/test_main_portfolio.py tests/test_phase_decision_guardrail.py`。测试使用虚构账户，不发送邮件、模型请求或交易。

整体 revert 本 PR，无数据库迁移。既有 Futu、手工账本与默认股票列表不依赖此模块的新入口；不要只移除观察护栏而保留导入分析入口。

## English reference

Phase one imports local Activity Flex XML snapshots, with no broker connection, ledger mutation or automated trading. Preview does not launch analysis or notifications. Explicit holding analysis reuses the existing task queue and always disables notifications; CLI retains its normal notification controls. No changes are made to scheduled GitHub jobs.

Single-account summary positions and base-currency cash are validated, retaining statement dates, FX basis and freshness. The cash-plus-positions denominator is not full broker NAV. Unsupported instruments remain visible in valuation but cannot masquerade as US stock analysis. Per-symbol context enters existing analysis and deterministic reports; execution suggestions are downgraded to observation until portfolio policies are implemented. No desktop UI/package, automatic synchronization or new GPT integration is shipped in this phase.
