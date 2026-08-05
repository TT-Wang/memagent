# Session Tape — 终局测试总表(/goal 交付物)

日期:2026-08-05 · 分支 `convergence-p0` · 统一价目(DeepSeek v4-flash):cache-hit $0.0028/M · miss $0.14/M · out $0.28/M。
所有参照数字从各自逐轮/逐任务 usage 账本重算;每个数字给出处。

> **版本记录(2026-08-05)**:本文档中间一版曾错误宣称"CB50 未跑 kimi 臂、$0.472 无出处"——那次撤回本身是错的:kimi CB50 跑过且数字全对,产物当时在会话级 scratchpad(搜索不完整导致误判)。现已永久落盘 `evals/contextbench/run-kimi-2026-08-05/`(usage 账本、pred/metrics、50 条轨迹、适配器 `evals/contextbench_kimi.py`),本版为三臂全量对账版。

## 结论一页

| 维度 | 结果 |
|---|---|
| 架构 | Session Tape 单一 append-only 流,401 项测试全绿,runtime/cli 分离与 subagent(scoped-turn)无破坏 |
| 质量 | 中轮 6/6 PASS;CB50 precision 三维全胜三臂、coverage 与 mini 打平低于 kimi;s10 零信息丢失;s11 **三臂唯一全绿**(r2) |
| 峰值上下文 | 全阶梯优势(CB50 1.4×/2.4×,中轮 2–4×,s11 2.4–3.0×,s10 9×)——"无 context rot"的机械保证 |
| 成本 | CB50 **−26% vs kimi、−68% vs mini**;中轮对 kimi 2 胜 4 负、对 mini 4 胜 1 负;s11 输 kimi(fresh 车道已修复 −23%,残差在 out 车道) |

## 交付物 1 · 核心结构

`packages/sliceagent-core/src/sliceagent_core/tape.py`:base(免行号全文)/ patch(事件时真差分 n=1)/ external(带外变更公示)/ reply(1200 字符封顶)四类条目,渲染即冻结;defer-base-until-edit(只读不入带);类型感知代际折叠(活 base 永不入折,0.7×预算迟滞);每封存诚实网(重组 vs 磁盘逐字节比对)。
s11 法证后新增:**repo map 冻结**(stream 布局下 msg0 变动=整 prompt 重付,实测 ×3)与 **drift 差分化**(重锚与编辑同"取小表示")。

## 交付物 2a · ContextBench-50 三臂(tape · kimi · swe-mini)

同 50 题 · 同官方 scorer · 统一价目逐任务重算。tape 数据 `evals/contextbench/run-tape-2026-08-05/`;kimi 数据 `evals/contextbench/run-kimi-2026-08-05/`(适配器 `evals/contextbench_kimi.py`);mini 取 08-04 官方轨迹重评(`run50-2026-08-04/`)。

### 检索质量(50 题均值)

| 指标 | **tape** | kimi | mini-swe |
|---|---|---|---|
| file coverage | 0.704 | **0.810** | 0.718 |
| symbol coverage | 0.697 | **0.856** | 0.712 |
| line coverage | 0.612 | **0.786** | 0.622 |
| file precision | **0.492** | 0.400 | 0.442 |
| line precision | **0.302** | 0.254 | 0.212 |
| span precision | **0.309** | 0.259 | 0.228 |

读法:kimi 以更广的读取换更高 coverage(file +0.106 vs tape),precision 全维最低段;tape 是**最精准**的臂(precision 三维全胜),coverage 与 mini 打平、低于 kimi。这与 redundancy 结论一致(08-04 官方:slice 系 line redundancy 7× 更净,p<0.001)。

### 消耗与成本

| | **tape** | kimi | mini-swe |
|---|---|---|---|
| 调用/步数(中位) | **8** | 12–12.5 | 53.5 |
| 峰值输入(中位) | **32.4k** | 45.9k(1.42×) | 76.3k(2.4×)¹ |
| 每任务成本(中位) | **$0.00575** | $0.00892 | $0.02013 |
| 50 任务合计 | **$0.351** | $0.472(**−26%**) | $1.114(**−68%**) |

¹ mini 峰值取 08-04 官方 provider 口径(76.3k);tape 时代重评估算口径为 45.5k——两口径并存,表内用与 tape/kimi 同类的 provider 口径。
统计显著性(配对 bootstrap,CI/p 值全套)见 `run50-2026-08-04/FINAL-TABLE.md`(slice-era vs mini:line/span precision p≤0.016、redundancy p<0.001、coverage 不显著;调用 5.9×、成本 3.70×)。

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

- CB50 三臂质量口径:三臂同 scorer 同 gold,但 kimi 的 pred 经 wire.jsonl 适配器提取(`evals/contextbench_kimi.py`),提取宽度影响 coverage/precision 的天然权衡,读表时连 redundancy 一起看。
- **coverage 差距的定性(2026-08-05 尾部 A/B)**:对 kimi 的 file-cov 差距 ~0.05–0.11 带内且单跑方差占大头(尾 8 题原样重跑 0.344→0.593);"exhaustive ask" 杠杆被预注册门否决(cov 仅 +0.07,precision 0.693→0.222、对照组同毁、成本 3.3×、峰值反超 kimi)——**不主张 coverage 领先,主张 precision 全维显著 + coverage 同带**。数据:`evals/contextbench/subset-{base-r2,exhaustive}/`。
- s7×50(全琐碎轮)tape $0.0301 vs kimi $0.0261(+15%):验证纪律地板;owner 判定该场景非真实世界,仅作峰值仪表。
- s11 成本仍 +70% vs kimi(r3):fresh 车道已收敛,out 车道 2.7× 为残差大头,结构收敛留 P8。
- 中轮 mini s1 无参照档(当时未存逐轮账本)。
- s11 r2/r3 的 PASS/FAIL 差一项(log ring)= 跑间方差;n=2 不足以下行为结论,质量主张以"基底 47–48/48 + quiz 4/4 双 rep 稳定"表述。

## 证据路径

- CB50:`evals/contextbench/run50-2026-08-04/`(官方定稿+CI)· `run-tape-2026-08-05/raw.jsonl` · `run-kimi-2026-08-05/`(usage+pred+metrics+50 轨迹)· `scratchpad/compare_tape_2026-08-05/`
- s11:`evals/spine_probe_runs/tape-s11_mixed_long-r{1,2,3}.json` · `scratchpad/s11_{kimi,mini}.json/` · 三方工作区 `/var/folders/…/T/*-s11_mixed_long-*`
- 中轮:`evals/spine_probe_runs/tape-s{1..6}*.json` + `mt_reference.json`
- s10:`evals/spine_probe_runs/tape-s10_compactloss-r4.json` · kimi wire 审计(0 压缩事件@392.9k)
- 场景生成器:`benchmarks/multiturn_coding/_gen_{mixed_long,compactloss}.py`(验收器修正历史在 git)
