# 成本参考价

## v0.3.1 官方价格快照

核对日期：2026-10-19，版本 `2026-10-19-v1`。价格固定打包，不在刷新时联网拉取。成本作为 API 参考值展示，历史也应用此快照；不代表 WorkBuddy 历史路由版本、服务端账单或订阅消费。币种只改变显示，固定 **1 USD = 6.8 CNY**；credit 始终为日志原值。

GPT 单位为 USD / 百万 token，顺序与 core.PRICES 一致：

| 模型 | 未缓存输入 | 缓存读取 | 输出 | 缓存写入 |
|---|---:|---:|---:|---:|
| gpt-6.1-sol | 2 | 0.10 | 10 | 2.50 |
| gpt-6-sol | 2 | 0.20 | 10 | 2.50 |
| gpt-6-astra | 10 | 1 | 50 | 12.50 |
| gpt-6-luna | 0.10 | 0.01 | 0.50 | 0.125 |
| gpt-5.6-sol | 4 | 0.40 | 20 | 5 |

本次核对的价格与已有 core.PRICES 一致，保留既有 >272K 长上下文和 Fast 规则，未重算 Codex 冻结费用。Standard/Fast 仅合并显示，仍先按请求计算费用再相加。WorkBuddy/CLI 中精确匹配这些 GPT 模型时采用 Standard 参考价，因日志没有明确的模式字段。

来源：[OpenAI 官方价格](https://developers.openai.com/api/docs/pricing)、[GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)、[GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol)、[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)。

DeepSeek 单位为 CNY / 百万 token：

| 模型参考 | 时段 | 未缓存输入 | 缓存读取 | 输出 |
|---|---|---:|---:|---:|
| Flash | 高峰 | 2 | 0.04 | 8 |
| Flash | 空闲 | 1 | 0.02 | 4 |
| Pro | 高峰 | 9 | 0.30 | 27 |
| Pro | 空闲 | 4.5 | 0.15 | 13.5 |

计算为 `((总输入-缓存)*未缓存价 + 缓存*缓存价 + 输出*输出价) / 1000000` 得到 CNY，再除 6.8 得到内部 USD。未知缓存不按零缓存计价；未知模型保留未知。费用保留精度，展示时才舍入。

Flash 精确映射 `deepseek-flash`、`deepseek-v4.1-flash`、`deepseek-v4.1-flash-1017`、`deepseek-v4-flash`、`deepseek-v4-flash-vision-exp`；Pro 映射 `deepseek-v4-pro`、`deepseek-v4-pro-0813`。官方当前版本为 V4.1-Flash-1017 / V4-Pro-0813；WorkBuddy 的内部模型名映射只用于参考价，不证明该工具实际调用的历史服务版本。其他近似名称不猜测。

按请求完成时间转换到北京时间：周一至周五 09:00–12:00、14:00–18:00 为高峰，排除 2026 节假日；周末和其他时段为空闲。起点包含、终点不包含。调休周末仍按官方明确的周一至周五规则为空闲。已保存假期为 1月1–3日、2月15–23日、4月4–6日、5月1–5日、6月19–21日、9月25–27日、10月1–7日。超出 2026 年且可能处于高峰时段时，缺少当年节假日依据，成本显示未知；明确的闲时仍可计算。

来源：[DeepSeek 官方模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[国务院 2026 节假日安排（北京市政府转载）](https://www.beijing.gov.cn/cs/gncs/zcwj/202603/t20260327_4568275.html)。

验证覆盖峰闲边界、周末、节假日、别名、固定汇率、未知值和混合汇总，以及合并模式后的费用合计。真实服务账单、平台 credit 与货币的换算、第三方模型价格及 API 额外工具费用未纳入。
