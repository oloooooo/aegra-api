---
type: concept
title: Behavior 与约束（Behavior & Constraints）
description: SBOL3 动态行为模型：事件、变量、条件块与空间约束声明。
tags: [sbol3, behavior, constraint, dynamic-model]
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

# Behavior 与约束

## Behavior：设计"做什么"

SBOL3 把动态行为挂在 Component 上，模块化子类型：

- `Event`：**离散动作**（表达、降解、迁移）。触发条件（`trigger`）+ 速率（`rate`）+ 前置条件（`precondition`）。
- `ContinuousVariable`：**连续变量**（浓度、荧光强度），带 `value` 与 `unit`。
- `DiscreteVariable`：**离散状态**（开关态、细胞代数）。
- `ConditionalEffect`：变量在条件满足时的效应。

组合语义：Event 触发时按 `rate` 改变 ContinuousVariable——典型基因线路（toggle switch、oscillator）都能只用这四类块声明。

## Constraint：空间约束

挂在 Feature 对之间，`restriction` 取值：

- `precedes` / `precededBy`：序列顺序（启动子必须 precede CDS）；
- `contains` / `containedIn`：包含关系；
- `meets` / `overlaps`：边界关系（多数设计校验器禁止 overlaps）。

## 与静态模型的分工

静态结构归 [Component 与 Feature 模型](component-and-feature.md)，动态行为归本页；两者通过 Component URI 关联，序列本身不进 Behavior——序列变更只改 Feature 位置，不动行为声明。
