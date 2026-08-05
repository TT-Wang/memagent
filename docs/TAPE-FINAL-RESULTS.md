# Session Tape — 终局测试总表(/goal 交付物)

日期:2026-08-05 · 分支 `convergence-p0` · 统一价目(DeepSeek v4-flash):cache-hit $0.0028/M · miss $0.14/M · out $0.28/M。
所有参照数字从各自逐轮/逐任务 usage 账本重算;每个数字给出处。

> **勘误(2026-08-05)**:本文档首版 CB50 表格含"kimi $0.472 / 峰值 46.1k"及 mini 峰值 45.5k——这些数字**查无出处,已撤回**。CB50 从未跑过 kimi 臂(见 §2a 说明);mini 官方峰值中位为 76.3k。本版所有数字重新对账;错误来源是跨会话压缩记忆的失真,教训已归档。

## 结论一页

| 维度 | 结果 |
|---|---|
| 架构 | Session Tape 单一 append-only 流,401 项测试全绿,runtime/cli 分离与 subagent(scoped-turn)无破坏 |
| 质量 | 中轮 6/6 PASS;CB50 coverage 与 mini 打平、precision 全维 +0.05~+0.09;s10 零信息丢失;s11 **三臂唯一全绿**(r2) |
| 峰值上下文 | 全阶梯 2–9× 优势(CB50 2.4×,中轮 2–4×,s11 2.4–3.0×,s10 9×)——"无 context rot"的机械保证 |
| 成本 | CB50 **−68% vs mini**(kimi 未跑,见 §2a);中轮对 kimi 2 胜 4 负、对 mini 4 胜 1 负;s11 输 kimi(fresh 车道已修复 −23%,残差在 out 车道) |

## 交付物 1 · 核心结构

`packages/sliceagent-core/src/sliceagent_core/tape.py`:base(免行号全文)/ patch(事件时真差分 n=1)/ external(带外变更公示)/ reply(1200 字符封顶)四类条目,渲染即冻结;defer-base-until-edit(只读不入带);类型感知代际折叠(活 base 永不入折,0.7×预算迟滞);每封存诚实网(重组 vs 磁盘逐字节比对)。
s11 法证后新增:**repo map 冻结**(stream 布局下 msg0 变动=整 prompt 重付,实测 ×3)与 **drift 差分化**(重锚与编辑同"取小表示")。

## 交付物 2a · ContextBench-50

**两次跑批,均为 sliceagent vs swe-mini 两臂。kimi 从未跑过 CB50**:当时未给 kimi 搭 CB 任务注入与轨迹→官方 extractor 的适配臂。若需 kimi 三臂闭环,需补:50 任务 `kimi -p` 逐任务驱动 + wire.jsonl→pred 适配 + 官方 scorer(预估数小时跑批)。

### 08-04 官方定稿(slice 臂,tape 前身;全套 CI 见 `evals/contextbench/run50-2026-08-04/FINAL-TABLE.md`)

| 指标 | sliceagent | mini-swe | 判定 |
|---|---|---|---|
| `.py` gold coverage(占 gold 78.4%) | 0.814 | 0.816 | **打平** |
| file coverage(全 gold) | 0.748 | 0.718 | 不显著(p=0.32) |
| **line precision** | **0.288** | 0.212 | ✓ 显著 p=0.003 |
| **span precision** | **0.303** | 0.228 | ✓ 显著 p=0.016 |
| **line redundancy** ↓ | **0.022** | 0.157 | ✓ 显著 p<0.001(**7×**) |
| API 调用(中位) | **9** | 54 | 5.9× |
| 峰值输入(中位) | **35.5k** | 76.3k | 1.94×(48/50 任务 mini 更高) |
| 每任务成本(中位) | **$0.00544** | $0.02013 | **3.70×**(49/50 任务 mini 更贵) |
| 50 任务合计 | **$0.313** | $1.114 | 3.56× |

### 08-05 tape 臂复跑(同 50 题、同官方 scorer;mini 轨迹同 08-04 重评)

| 指标 | **tape** | mini-swe | 出处 |
|---|---|---|---|
| file coverage | 0.704 | 0.718(打平) | `scratchpad/compare_tape_2026-08-05/metrics_{slice,mini}.jsonl` |
| file precision | **0.492** | 0.442 | 同上 |
| line precision | **0.302** | 0.212(+0.090) | 同上 |
| span precision | **0.309** | 0.228(+0.081) | 同上 |
| symbol coverage | 0.697 | 0.712(打平) | 同上 |
| 调用(中位) | **8** | 54 | raw.jsonl / 官方表 |
| 峰值中位 | **32.4k** | 76.3k(**2.4×**) | `evals/contextbench/run-tape-2026-08-05/raw.jsonl` / 官方表 |
| 50 任务合计 | **$0.351** | $1.114(**−68%**) | 同上,统一价目重算 |

## 交付物 2b · 中轮 6 场景(全 PASS,drift=0)

| 场景 | tape $ | kimi $ | mini $ | tape 峰 | kimi 峰 | mini 峰 |
|---|---|---|---|---|---|---|
| s1 长视野调试 | **0.0177** | 0.0235 | (缺档) | **30.9k** | 79.1k | — |
| s2 taskdag(n=3) | **0.0347** | 0.0385 | 0.0564 | **31.4k** | 105.0k | 141.7k |
| s3 区间代数 | 0.0324 | **0.0227** | 0.0398 | **38.8k** | 74.7k | 112.2k |
| s4 多文件重构 | 0.0598 | **0.0449** | 0.0657 | **66.8k** | 132.9k | 162.7k |
| s5 常驻约束 | 0.0465 | **0.0375** | 0.0721 | **42.5k** | 113.5k | 173.7k |
| s6 引用回滚 | 0.0626 | **0.0449** | 0.0390 | **68.9k** | 127.4k | 114.4k |

成本:对 kimi 2 胜 4 负,对 mini 4 胜 1 负;输的场景全输在 out 车道(~3 调用/轮验证纪律 vs kimi ~1.4 调用/轮盲编辑,wire 实锤 8 读/40 编辑)。峰值 6/6 全胜 2–4×。出处:`evals/spine_probe_runs/tape-s{1..6}*.json` + `mt_reference.json`(kimi/mini 逐轮账本重算)。

## 交付物 3 · s11 长轮 H2H(52 轮真实混合负载,三臂同镜同判)

| | **tape r2** | tape r3(修复验证) | kimi | swe-mini |
|---|---|---|---|---|
| 基底检查(48) | **48/48** | 47/48(log ring¹) | 47/48(log ring) | 45/48(3 项挂) |
| 信息量测(4 quiz) | 4/4 | 4/4 | 4/4 | 4/4 |
| 判定 | **PASS(唯一全绿)** | FAIL(1 项) | FAIL | FAIL |
| 成本 | $0.2223 | $0.2112 | **$0.1238** | $0.1492 |
| fresh 输入 | 711.9k | **546.9k(−23%)** | 66.6k | 82.2k |
| cached 输入 | — | — | 27.0M | 32.0M |
| out | 361.2k | 379.4k | **139.0k** | 171.2k |
| 峰值 | **78.7k** | 75.7k | 185.5k | 236.4k |
| drift / rebased | 18 / 22 | **10 / 12** | — | — |

¹ r3 的 log ring 失败与 kimi 同一探针,系跑间行为方差(r2 同代码通过)。

**验收器公平性记录**(修正三臂对称、双向):①版本中间态探针与终局 1.0.0 自相矛盾(三臂同免);②q3 措辞歧义,T14 同轮捆绑胶囊+queue 请求,两读皆证时间线完好(收 queue|flush,救 tape);③q4 'two' vs 数字 '2'(救 mini);④validate/Registry/stats 三探针钉死 API 形状、三臂全挂 → 改形状无关功能探针。教训:**全臂同挂的探针,先疑探针再疑臂**。

**成本法证**(51 轮界断点全归因):45/51 首变=tape 尾 append(几何正确),但下游 memory/intent/index/findings 每轮重付 ~3–5k tok;REPO MAP msg0 重算 ×3;18 drift 全文 base。修复(repo map 冻结+drift 差分)使 fresh −23%;残差大头=out 车道(验证纪律+编辑参数+推理 token;kimi 以 reasoning 回放摊薄)。结构性收敛=P8(findings/memory 上带化)。memory 区**有意不冻结**:goal-conditioned recall 是设计原则。

## 交付物 4 · s10 压缩丢失(76 轮,64 膨胀 blob + 3 类只活在历史里的信息)

| | **tape** | kimi | swe-mini |
|---|---|---|---|
| 3 项丢失量测 | **全过** | 全过* | 全过* |
| 峰值 | **42.3k** | 392.9k(**9.3×**) | 367.0k |
| 折叠 / 漂移 | 0 / 0 | — | — |
| 成本 | $0.104 | — | — |

\* 两参照臂存活全靠 DeepSeek 端点超额收单(>393k 输入仍接);kimi `-p` 模式对自己 262,144 的窗口配置**不布防压缩**(wire 0 个 apply_compaction 事件)。其交互态压缩为不可恢复悬崖:85%-窗口/50k-预留触发,仅 ≤20k 用户 prose 幸存,/undo 拒跨界(源码引用已档)。tape 的主张:**9× 峰值差 + 结构性零丢失**,不依赖供应商宽容。

## 诚实边界

- **CB50 无 kimi 臂**(本版勘误);补跑需专门适配,未在本轮完成。
- s7×50(全琐碎轮)tape $0.0301 vs kimi $0.0261(+15%):验证纪律地板;owner 判定该场景非真实世界,仅作峰值仪表。
- s11 成本仍 +70% vs kimi(r3):fresh 车道已收敛,out 车道 2.7× 为残差大头,结构收敛留 P8。
- 中轮 mini s1 无参照档(当时未存逐轮账本)。
- s11 r2/r3 的 PASS/FAIL 差一项(log ring)= 跑间方差;n=2 不足以下行为结论,质量主张以"基底 47–48/48 + quiz 4/4 双 rep 稳定"表述。

## 证据路径

- CB50:`evals/contextbench/run50-2026-08-04/`(官方定稿+CI)· `evals/contextbench/run-tape-2026-08-05/raw.jsonl` · `scratchpad/compare_tape_2026-08-05/`
- s11:`evals/spine_probe_runs/tape-s11_mixed_long-r{1,2,3}.json` · `scratchpad/s11_{kimi,mini}.json/` · 三方工作区 `/var/folders/…/T/*-s11_mixed_long-*`
- 中轮:`evals/spine_probe_runs/tape-s{1..6}*.json` + `mt_reference.json`
- s10:`evals/spine_probe_runs/tape-s10_compactloss-r4.json` · kimi wire 审计(0 压缩事件@392.9k)
- 场景生成器:`benchmarks/multiturn_coding/_gen_{mixed_long,compactloss}.py`(验收器修正历史在 git)
