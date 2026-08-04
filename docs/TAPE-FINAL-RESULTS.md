# Session Tape — 终局测试总表(/goal 交付物)

日期:2026-08-05 · 分支 `convergence-p0` · 统一价目(DeepSeek v4-flash):cache-hit $0.0028/M · miss $0.14/M · out $0.28/M。
Kimi/mini 参照一律从各自逐轮 usage 账本重算,绝无换算近似。原始数据路径见文末。

## 结论一页

| 维度 | 结果 |
|---|---|
| 架构 | Session Tape 单一 append-only 流,731 行核心,401 项测试全绿,runtime/cli 分离与 subagent(scoped-turn)无破坏 |
| 质量 | 中轮 6/6 PASS;CB50 与 mini 打平、precision 全维更高;s10 零信息丢失;s11 **三臂唯一全绿** |
| 峰值上下文 | 全阶梯 2–9× 优势(中轮 2–4×,s11 2.4–3.0×,s10 9×)——这是"无 context rot"的机械保证 |
| 成本 | CB50 −26% vs kimi;中轮互有胜负(s1/s2 赢,s3–s6 输在验证纪律的 out 车道);s11 r2 输(法证归因完毕,修复已并,r3 验证中) |

## 交付物 1 · 核心结构

`packages/sliceagent-core/src/sliceagent_core/tape.py`:base(免行号全文)/ patch(事件时真差分 n=1)/ external(带外变更公示)/ reply(1200 字符封顶)四类条目,渲染即冻结;defer-base-until-edit(只读不入带);类型感知代际折叠(活 base 永不入折,0.7×预算迟滞);每封存诚实网(重组 vs 磁盘逐字节比对)。
本轮法证新增两修复:**repo map 冻结**(stream 布局下 session 首算即冻——msg0 变动=整 prompt 重付,s11 实测 ×3)与 **drift 差分化**(honesty-net 重锚与编辑同"取小表示",18 次全文 base 是重锚字节大头)。

## 交付物 2a · ContextBench-50 三臂

| | tape | kimi | swe-mini |
|---|---|---|---|
| file coverage | 0.704 | — | 0.718(打平) |
| precision(全四维) | **+0.05~+0.09** | 基线 | 基线 |
| redundancy | **6–8× 更净** | — | 基线 |
| 收敛步数 | **8** | — | 54 |
| 成本 | **$0.351** | $0.472(−26%) | — |
| 峰值中位 | **32.4k** | 46.1k | 45.5k |

## 交付物 2b · 中轮 6 场景(全 PASS,drift=0)

| 场景 | tape $ | kimi $ | mini $ | tape 峰值 | kimi 峰值 | mini 峰值 |
|---|---|---|---|---|---|---|
| s1 调试长视野 | **0.0177** | 0.0235 | (缺档) | **30.9k** | 79.1k | — |
| s2 taskdag(n=3) | **0.0347** | 0.0385 | 0.0564 | **31.4k** | 105.0k | 141.7k |
| s3 区间代数 | 0.0324 | **0.0227** | 0.0398 | **38.8k** | 74.7k | 112.2k |
| s4 多文件重构 | 0.0598 | **0.0449** | 0.0657 | **66.8k** | 132.9k | 162.7k |
| s5 常驻约束 | 0.0465 | **0.0375** | 0.0721 | **42.5k** | 113.5k | 173.7k |
| s6 引用回滚 | 0.0626 | **0.0449** | 0.0390 | **68.9k** | 127.4k | 114.4k |

成本互有胜负的解剖(s7×50 同因):输掉的场景全输在 **out 车道**——我们 ~3 调用/轮跑验证,kimi ~1.4 调用/轮盲编辑(wire 实锤 8 读/40 编辑)。这是品质地板不是浪费;fresh 车道我们全线更低或持平。

## 交付物 3 · s11 长轮 H2H(52 轮真实混合负载)

| | tape r2 | kimi | swe-mini |
|---|---|---|---|
| 基底检查(48) | **48/48** | 47/48(log ring 挂) | 45/48(graph purge/log ring/json export 挂) |
| 信息量测(quiz 4) | 4/4 | 4/4 | 4/4 |
| 判定 | **PASS(唯一全绿)** | FAIL | FAIL |
| 成本 | $0.2223 | **$0.1238** | $0.1492 |
| 峰值 | **78.7k** | 185.5k | 236.4k |
| 曲线 | 91.2% 中位 / 96% 尾部 | (回放 27.0M cached tok) | (回放 32.0M cached tok) |

**验收器公平性记录**(修正三臂对称、双向,先于任何一臂重判固定):①版本中间态探针与终局 1.0.0 自相矛盾(三臂同免);②q3 措辞歧义,T14 同轮捆绑胶囊+queue 请求,两读皆证时间线完好(收 queue|flush,救 tape);③q4 'two' vs 数字 '2'(救 mini);④validate/Registry/stats 三探针钉死 API 形状、三臂全挂 → 改形状无关功能探针(三臂同免)。教训:**全臂同挂的探针,先疑探针再疑臂**。

**s11 成本法证**(51 个轮界断点全归因):45/51 首变=tape 尾 append(几何正确),但 tape 下游压着 memory/intent/index/findings 准稳定区,每轮全额重付 ~3–5k tok;REPO MAP 在 msg0 被重算 ×3=整 prompt 重付;18 次 drift 全文 base。已修:repo map 冻结 + drift 差分。**r3 验证跑中**;剩余结构项=tape 下游 volatile tail(P8:findings/memory 上带化)。memory 区**有意不冻结**:goal-conditioned recall 是设计原则,冻结=杀推送通道。

## 交付物 4 · s10 压缩丢失(76 轮,64 膨胀 blob + 3 类只活在历史里的信息)

| | tape | kimi | swe-mini |
|---|---|---|---|
| 3 项丢失量测 | **全过** | 全过* | 全过* |
| 峰值 | **42.3k** | 392.9k | 367.0k |
| 成本 | $0.104 | — | — |
| 折叠/漂移 | 0 / 0 | — | — |

\* kimi/mini 存活全靠 DeepSeek 端点超额收单(>393k 输入)——kimi `-p` 模式对自己 262,144 的窗口配置**不布防压缩**(wire 0 个 apply_compaction 事件)。其交互态压缩是阻塞悬崖:85%-窗口/50k-预留触发,仅 ≤20k 用户 prose 幸存、不可恢复(源码引用已档)。tape 的主张:**9× 峰值差 + 结构性零丢失**,不依赖供应商宽容。

## 诚实边界

- s7×50(全琐碎轮)tape $0.0301 vs kimi $0.0261(+15%):验证纪律地板;user 判定该场景非真实世界,仅作峰值仪表。
- s11 r2 成本反输 79%;修复已并、r3 在跑,本表交付时以 r3 实测为准补一行。
- out 车道在真实负载 2.6×(361k vs 139k):编辑参数+验证探针+推理 token;结构收敛需后续(kimi 靠 reasoning 回放摊薄)。
- 中轮 mini s1 无参照档(当时未存逐轮账本)。

## 证据路径

- s11:`evals/spine_probe_runs/tape-s11_mixed_long-r{1,2}.json` · kimi/mini 结果+逐轮账本 `scratchpad/s11_{kimi,mini}.json/` · 三方工作区 `/var/folders/…/T/{bench,kimi-arm,mini-arm}-s11_mixed_long-*`
- CB50:`scratchpad/compare_tape_2026-08-05/`(metrics_*.jsonl, comparison.json)
- 中轮:`evals/spine_probe_runs/tape-s{1..6}*.json` + `evals/spine_probe_runs/mt_reference.json`
- s10:`evals/spine_probe_runs/tape-s10_compactloss-r4.json` · kimi wire 审计(0 压缩事件@392.9k)
- 场景生成器:`benchmarks/multiturn_coding/_gen_{mixed_long,compactloss}.py`(验收器修正历史在 git)
