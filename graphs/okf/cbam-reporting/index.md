---
okf_version: "0.2"
---

# CBAM 碳关税申报知识包

欧盟碳边境调节机制申报业务逻辑。核心问题：实测还是默认值核算、清缴多少证书、什么情况被罚。

## 概念

- [申报主体与货物判定](declarant-and-goods.md) —— 申报人义务与 CN 编码管制范围
- [嵌入排放核算](embedded-emissions.md) —— 实测/默认值决策树与惩罚系数
- [证书清缴与罚则](certificate-and-surrender.md) —— 清缴比例、罚则与 Omnibus 简化

## 数据集

- `cn_goods_rules.xlsx`
  - 「默认排放因子」：14 类管制货物因子（配合[嵌入排放核算](embedded-emissions.md)）
  - 「申报校验规则」：8 条校验逻辑（配合[申报主体与货物判定](declarant-and-goods.md)）

## 记录

- [log.md](log.md) —— 更新历史
