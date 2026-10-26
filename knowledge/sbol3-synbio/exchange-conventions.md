---
type: concept
title: 交换与版本约定（Exchange Conventions）
description: SBOL3 设计交换的 URI/displayId 命名规则与 Component/VariableDerivation 派生关系。
tags: [sbol3, uri, versioning, exchange]
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

# 交换与版本约定

## URI 命名（最强约定）

每个对象必须有全局唯一 URI，惯例形状：

```
<namespace>/<displayId>          对象自身
<parent-uri>/<displayId>         子对象（Feature 挂在 Component 下）
```

- `namespace` 由设计者所在机构控制（域名反转是惯例）；
- **displayId 只用 [a-zA-Z0-9_]**，不得含连字符/中文/空格——交换到多数自动化平台时会被拒收。

## displayId 的语义约定

displayId 不是自由标签：同一个设计在不同机构间流转时，**序列没变则 displayId 必须保持**，否则下游比对失效。

## 版本派生（CD → VD）

- `ComponentDefinition` 已在 SBOL3 废弃，现行模型只有 `Component`；
- 设计迭代不新建孤立 Component，而用 `VariableDerivation`（VD）声明派生：母版 CD → 变体 VD（记录突变/替换的 Feature）；
- 好处：序列比对、谱系追溯都走 VD 链，见 [Component 与 Feature 模型](component-and-feature.md) 的嵌套规则。

## 交换检查清单

1. URI 全局唯一且 displayId 合法；
2. 所有 SO 编号有效（对照 [Component 与 Feature 模型](component-and-feature.md)）；
3. Behavior 引用的 Feature URI 在同一文档内可解析；
4. RDF 序列化用 Turtle（.ttl），不用 RDF/XML（各平台兼容性最差）。
