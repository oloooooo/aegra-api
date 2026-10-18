---
type: concept
title: 表面活化（Surface Activation）
description: 等离子体活化工艺对晶圆表面能与亲水性的影响规律。
tags: [hybrid-bonding, plasma, surface-energy]
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

# 表面活化

活化的目的是让 Cu 与介质层表面在常温预键合时具备足够表面能（>70 mJ/m²）形成初始键合。

## 参数效应

- **功率**：主导因素。功率过低活化不充分；功率超过 300 W 会损伤介质层表面（形成富碳层），反而降低亲水性。
- **时间**：与功率近似等效（剂量 = 功率 × 时间），但长时低功率优于短时高功率——表面损伤更小。
- **气体配比**：O₂/N₂ 混气优于纯 O₂（纯 O₂ 过氧化表面）。

## 关联

活化后表面能随时间衰减，间隔时间必须与 [键合窗口](bonding-window.md) 的预键合阶段一起评估；衰减的表征手段见 [退火与表征](anneal-and-characterization.md) 的接触角法。

DOE 中活化功率的取值与对应良率见 `键合工艺DOE.xlsx`。
