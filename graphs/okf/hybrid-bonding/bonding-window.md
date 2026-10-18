---
type: concept
title: 键合窗口（Bonding Window）
description: 混合键合三阶段（表面活化、预键合、退火）的参数边界与耦合关系。
tags: [hybrid-bonding, process-window]
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

# 键合窗口

混合键合的工艺窗口由三个先后衔接的阶段共同决定，任一阶段出界都会以特定缺陷形式暴露在最终良率上。

## 三阶段与参数

| 阶段 | 关键参数 | 典型窗口 | 出界后果 |
|------|---------|---------|---------|
| 表面活化 | 等离子体功率、照射时间 | 见 [表面活化](surface-activation.md) | 表面能不足 → 预键合不起粘 |
| 预键合 | 温度、压力、对准精度 | 20-25 °C 常温；压力 1-5 kN | 压力过高 → 界面空洞；过低 → 局部未贴合 |
| 退火 | 温度、时间、升降温柔变率 | 见 [退火与表征](anneal-and-characterization.md) | 升温过快 → 界面滑移/凸块偏移 |

## 参数耦合（易踩坑）

1. **活化-退火耦合**：表面能随放置时间衰减（hydrophobic recovery）。活化后到预键合的间隔超过 30 min，即使退火参数不变，键合强度也会显著下降。
2. **压力-空洞耦合**：预键合压力与界面空洞率近似单调——压力每上调 1 kN，空洞率约降 0.3 pct，但超过 4 kN 后颗粒压入风险陡增。
3. **退火温度-电阻耦合**：Cu-Cu 扩散接合在 250 °C 以上才进入快速段；低于 200 °C 退火即使键合强度达标，接触电阻也不收敛。

## 数据

DOE 参数与良率的实测对照见 `键合工艺DOE.xlsx`（sheet「DOE结果」），缺陷归因见 [退火与表征](anneal-and-characterization.md)。
