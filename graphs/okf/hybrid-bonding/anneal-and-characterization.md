---
type: concept
title: 退火与表征（Anneal & Characterization）
description: 退火曲线设计原则与键合强度/缺陷的表征判据。
tags: [hybrid-bonding, anneal, metrology]
timestamp: 2026-10-20
status: stable
verified: true
generated:
  by: okf-enrichment-agent
  at: 2026-10-20
stale_after: 2027-04-20
sources:
  - "https://doi.org/10.1109/ECTC.2019.00034  # 公开文献量级参照（合成数据）"
  - "internal://demo-doe  # scripts/gen_okf_demo_data.py 生成的演示 DOE"
---

# 退火与表征

## 退火曲线设计

1. **低温段（<150 °C）缓升**：驱除界面吸附水。升温速率 >5 °C/min 时水汽爆发式逸出，形成界面空洞（缺陷代码 V1）。
2. **中温段（150-200 °C）保温**：介质层流动愈合界面。此段决定空洞率的收敛速度。
3. **高温段（250-300 °C）接合**：Cu-Cu 扩散成键。低于 200 °C 接触电阻不收敛，见 [键合窗口](bonding-window.md) 的耦合说明。

## 表征判据

| 指标 | 方法 | 合格线 |
|------|------|--------|
| 键合强度 | 临界能量释放率（双悬臂梁 DCB） | ≥1.5 J/m² |
| 界面空洞率 | 超声扫描显微镜（SAM） | ≤1% |
| 接触电阻 | 四点探针 / TLM 结构 | ≤5 mΩ·cm² |
| 表面能（活化后） | 水接触角 | 接触角 <20°，见 [表面活化](surface-activation.md) |

## 缺陷分类

6 类典型缺陷的代码、判定方法与致命性登记在 `键合工艺DOE.xlsx`（sheet「缺陷分类」）。归因路径：先看 SAM 空洞形貌定位缺陷代码，再回查 [键合窗口](bonding-window.md) 中对应阶段的参数是否出界。
