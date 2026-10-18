---
type: concept
title: 嵌入排放核算（Embedded Emissions）
description: CBAM 管制货物嵌入排放的实测与默认值选择逻辑、惩罚系数。
tags: [cbam, embedded-emissions, default-values]
timestamp: 2026-10-20
status: stable
verified: false
generated:
  by: okf-enrichment-agent
  at: 2026-10-20
stale_after: 2027-01-31
sources:
  - "https://eur-lex.europa.eu/eli/reg/2023/956  # CBAM 条例"
  - "https://eur-lex.europa.eu/  # 2025 Omnibus 简化修订（公开文本）"
  - "internal://demo-data  # 排放因子为合成演示数据"
---

# 嵌入排放核算

嵌入排放 = **直接排放**（生产过程燃料+工艺排放）+ **间接排放**（外购电力对应排放）。仅电力货物只计间接排放的等价口径。

## 实测 vs 默认值（决策树）

1. 生产设施已在 CBAM 登记册登记且提交过经核证的实际排放数据 → **必须用实测值**；
2. 未登记/首次进口年度 → 可用**默认值**（见 `cn_goods_rules.xlsx` sheet「默认排放因子」）；
3. 同一货物既有实测又有默认值 → 用实测；
4. 使用默认值的，其碳价按生产国已付碳价抵扣——抵扣需提供**第三国碳价支付凭证**。

## 惩罚系数

用默认值申报的货物，清缴时按 **默认值 × 1.5** 的系数折算证书需求（惩罚系数随年份退坡，2026 年为 1.5）。含义：默认值只是兜底，长期依赖会被罚出价格优势。

## 与其他概念的关系

- 默认值适用性受 [申报主体与货物判定](declarant-and-goods.md) 的 50 t 豁免阈值影响；
- 核算结果进入 [证书清缴与罚则](certificate-and-surrender.md) 的年度清缴基数。
