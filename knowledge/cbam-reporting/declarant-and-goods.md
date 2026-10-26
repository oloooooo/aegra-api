---
type: concept
title: 申报主体与货物判定（Declarant & Goods Scope）
description: CBAM 申报人的资格义务与管制货物的 CN 编码范围判定。
tags: [cbam, declarant, cn-code, scope]
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

# 申报主体与货物判定

## 谁是申报人（Declarant）

- 申报人必须是**在欧盟设立**的进口商，或其**间接申报人**（non-Union declarant，须有授权代表 + 保证金）。
- 年进口管制货物**总质量 <50 t** 的小进口商豁免（2025 Omnibus 提高阈值，原按金额阈值）。
- 申报人资格经成员国主管机关授权后获得，授权有效期与 CBAM 登记号绑定。

## 货物范围

管制货物按 CN 编码判定，覆盖 6 个部门：水泥、电力、化肥、钢铁、铝、氢。CN 8 位编码级的完整示例清单及默认排放因子见 `cn_goods_rules.xlsx`（sheet「默认排放因子」）。

判定规则：
1. 按进口报关单上的 CN 8 位编码匹配，命中即管制；
2. 子目经过 Omnibus 扩展的复合产品（如含钢的机械零件）按**全质量**申报，不按净钢材质量折算；
3. 电力的判定不看质量，看**进口电量 kWh**。

## 申报节奏

- 季度：进口后 1 个月内提交进口申报（含 CN 编码、质量、排放数据来源）。
- 年度：次年 5 月 31 日前提交年度报告（含实际嵌入排放的修正值）。
- 数据质量校验规则见 `cn_goods_rules.xlsx`（sheet「申报校验规则」），违规等级 B 以上的申报会被退回。
