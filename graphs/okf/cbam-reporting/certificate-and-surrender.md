---
type: concept
title: 证书清缴与罚则（Certificate Surrender & Penalties）
description: CBAM 证书的购买、年度清缴比例与逾期罚则。
tags: [cbam, certificates, penalty, surrender]
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

# 证书清缴与罚则

## 证书生命周期

1. CBAM 证书自 2027 年 2 月起开放销售（对应 2026 年度排放）；
2. 每张证书 = 1 tCO₂e，价格挂钩欧盟 ETS 周拍卖均价（周度更新）；
3. 证书当年有效，最多可囤 2 年后失效作废。

## 年度清缴比例

申报人在次年清缴时，证书覆盖的排放量按年度递增：

| 排放年度 | 清缴比例 | 说明 |
|---------|---------|------|
| 2026 | 50% | 确定期首年，宽松 |
| 2027 | 75% | 过渡档 |
| 2028 起 | 100% | 与 ETS 全额对齐 |

清缴基数 = 实际嵌入排放（含 [嵌入排放核算](embedded-emissions.md) 的默认值惩罚系数）。

## 罚则

- 逾期未清缴：罚 **€100/tCO₂e**（按通胀指数化），且**不免除**次年补缴义务；
- 申报数据造假：除退回申报外，暂停申报人资格 1 个清缴年度；
- 间接申报人保证金不足：进口报关单被海关拦截。
