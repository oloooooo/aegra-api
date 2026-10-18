---
type: concept
title: Component 与 Feature 模型
description: SBOL3 静态设计模型：Component 类型层次、Feature 子类型与 SO 术语对照。
tags: [sbol3, component, feature, sequence-ontology]
timestamp: 2026-10-20
status: stable
verified: true
generated:
  by: okf-enrichment-agent
  at: 2026-10-20
stale_after: 2027-10-20
sources:
  - "https://sbolstandard.org/datamodel-specification/  # SBOL3 数据模型规范"
  - "https://github.com/SynBioDex/SBOLSpec  # SO 术语编号"
---

# Component 与 Feature 模型

## Component：设计的顶层单元

`Component` 是一切设计的容器，`types` 字段取 SO（Sequence Ontology）编号：

| types (SO) | 含义 | 常见例子 |
|-----------|------|---------|
| SO:0000167 | promoter | pTet、J23100 |
| SO:0000139 | RBS | B0034 |
| SO:0000316 | CDS | sfGFP、T7 RNA pol |
| SO:0000141 | terminator | B0015 |
| SO:0000704 | gene | 启动子+RBS+CDS 组合件 |

**坑**：gene 与 CDS 不可混用——gene 含调控区，CDS 只含编码序列；序列本体里两者 SO 编号不同，混用会让下游翻译工具解析失败。

## Feature：Component 内部的标注

Feature 不持有序列，只持有在宿主序列上的**位置**（`locations`）：

- `SubComponent`：引用另一个 Component 实现嵌套（质粒 = backbone + insert 两个 SubComponent）；
- `SequenceFeature`：泛化标注（如标签区、突变位点）；
- `Constraint`：声明 Feature 间的空间/顺序关系（见 [Behavior 与约束](behavior-and-constraints.md)）。

## 嵌套规则

Feature 引用的 SubComponent 可以再含 Feature（三层以上常见），但**不允许环**——SBOL3 用 URI 引用，环引用在校验期报错。派生关系（CD → VD）见 [交换与版本约定](exchange-conventions.md)。
