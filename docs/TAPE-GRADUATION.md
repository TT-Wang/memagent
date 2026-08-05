# Tape 毕业判据 + 删除清单(预注册,2026-08-05)

Owner: TT-Wang。本文档是 P-T4/P-T5 的决策仪器:四门全绿 → tape 毕业为**唯一架构**,
按下方清单执行两波负 diff;任一门红 → 修复后重跑该门,**绿之前不删任何东西**。

裁决对象是 **tape vs 它的前代**(spine / locators / off),不是 tape vs kimi——对 kimi 的
成本残差(out 车道、P8)是 tape 架构内部的后续优化,不扣押毕业。前代裁决证据已齐:
spine 曲线封顶 33.6%(P5 verdict)、locators 经济性 −22%(oflocators verdict)、off 无界。

## A. 毕业门(预注册;数据落地即判,不重新议门)

| 门 | 判据 | 数据源 | 状态 |
|---|---|---|---|
| **G1 typed-core 字节门(中程)** | s2 r8:PASS · drift=0 · 同轮完整性 100% · tape_chars 回到 r4–r6 带(≤45k)或 r7+r8 均值 ≤48k(方差判);成本 ≤ $0.050 | `evals/spine_probe_runs/tape-s2_taskdag_scheduler-r8.json` | 跑中(pid 62634) |
| **G2a 长程质量(活模型,已判)** | s11 typed r4:公平镜 PASS 级质量 | r4 = **48/48 + quiz 4/4、曲线 91.0%、同轮 125/125** ✅ | ✅ 绿 |
| **G2b 长程机制(离线重放)** | `evals/tape_replay.py` s11 形态 52 轮:folds ≤3 · final ≤ 预算 · 无 thrash · 轮界账单中位 <6k | r4 抓到净增折叠回归(18 折/166k)→ 净效应裁剪修复 → 重放 **folds 2 / 86.3k / 无相邻折 / 中位 1.6k** ✅;常驻 pytest 门 | ✅ 绿 |
| **G2c 毕业时刻活确认(一次性)** | 波一合并前跑**一次** s11 typed(fresh ≤600k 复核);此后机制迭代永不再用活模型 | 待跑(owner 择时,~45min/$0.24) | ⏳ 择时 |
| **G3 flag-off 字节恒等** | off 模式 golden(test_region_registry)在 HEAD 全绿——删除波之前 off 仍是逐字节 HEAD 语义 | `packages/sliceagent-core/tests/test_region_registry.py` | ✅(398 全绿含此) |
| **G4 全量门禁** | 全仓 pytest + ruff 全净 | CI/本地 | ✅ 398 + clean |

**若 G1 判"方差"**(r8 回带)→ 过;**若 r8 仍 >48k** → 先按条目类型分解 tape 体积
(digest/base/patch/reply 逐类字节),修复根因,重跑 G1。G2 同理:fresh 超带先做轮界断点
归因再修。

## B. 删除清单

### 存活件(先钉死,防误删)

| 件 | 为何存活 |
|---|---|
| `spine.py` 模块(`render_turn_digest` / `load_session_spine` / `_ASK_CAP_CHARS`) | 封存基底:runtime_persistence 的 seal digest 渲染、tape 的 digest 兜底渲染、崩溃恢复扫描都在用。死的是 spine **区**,不是 digest 器件 |
| ~~`session.session_spine` 缓存链~~ | **已在评审响应中删除**(Task148 f5:无消费者路径;artifact 即唯一摘要库,tape 经 load_session_digests 直读)|
| `render_file_locators`(seed.py) | 已转正:tape 模式的 OPEN FILES 哈希索引渲染器(组合契约现值指针) |
| `_SPINE_LAYOUT_SLOTS` 的布局思想 | 波一保留(改名 `_TAPE_LAYOUT_SLOTS`,删 "session_spine" 键);三区装配器落地后整体退役 |
| spine_probe.py | 通用测量仪(label 只是标签),tape 的持续度量工具 |

### 波一(毕业即执行)——删实验模式,`stream_mode()` 三态 → 布尔

| # | 目标 | 文件:位置 | 动作 |
|---|---|---|---|
| 1 | `AGENT_SESSION_SPINE` flag | regions.py:175–189(stream_mode 'spine' 臂)· seed.py(p3-value 支持段)· benchmarks/run.py(spine-parity else 分支) | 删分支;stream_mode() 收缩为 `tape_on()` 布尔(AGENT_SESSION_TAPE **默认 ON**,`=0` 为 kill switch) |
| 2 | `session_spine` RegionSpec | regions.py:985–991 + `_SPINE_LAYOUT_SLOTS["session_spine"]`(:1252)+ context_compiler `_ALWAYS` 中 "session_spine" | 删区声明与选择项 |
| 3 | `AGENT_OPENFILES_LOCATORS` 独立 flag | seed.py:642 分支条件 | 条件只留 tape_on();flag 字符串死;渲染器存活(见上) |
| 4 | 九处 `stream_mode()` 调用点 | regions.py:175,189,973,990,1007,1013,1025,1273 · seed.py:478 | 全部改 `tape_on()`;'spine' 值路径死 |
| 5 | 测试:`tests/test_session_spine.py`(324 行) | 全文件 | 区/模式测试删除;digest 渲染器与 load_session_spine 的测试**迁移**到新 `tests/test_spine_substrate.py`(≈60 行存活) |
| 6 | 测试:`tests/test_openfiles_locators.py`(82 行) | 全文件 | flag 测试删除;locator 渲染器断言并入 test_session_tape.py(≈20 行存活) |
| 7 | 测试:`test_region_registry.py` goldens | 逐 case | tape-on golden 转**主 golden**;off golden 保留(kill switch 语义)、spine/locators golden 删除 |
| 8 | `evals/oflocators_verdict.py`(109 行) | 全文件 | 删除(verdict 已归档于 git tag;结论进 memory) |
| 9 | bench/探针文档中的 spine/off 臂说明 | spine_probe.py docstring · benchmarks/run.py 注释 | 改为"历史臂在 tag `lab-2026-08-05` 复跑" |
| 10 | 文档 | SESSION-SPINE-ROADMAP/-MAP/-DESIGN + OPENFILES-SUBSUMPTION-DESIGN | 头部盖"RETIRED → superseded by tape"戳,正文不动(证据链) |

预计净 diff:**−700~−900 行**(不含文档)。

### 波间(独立提交,可与波一同 sprint)

| 项 | 内容 |
|---|---|
| 三区装配器 | build_context_blocks 的槽位仿真 → 原生 `[system][TAPE][TAIL]` 三区;新区**类型上**不可能落在 TAPE 上游(REPO MAP 类事故变编译错);预期跑分零变化,收益=结构防错 |
| P4 单车道编译器 | `compile_active_context` 改块生产者,终结"两个选择器喂一个渲染器"的最后重复路径 |

### 波二(一个干净发布周期后)

| # | 目标 | 动作 |
|---|---|---|
| 1 | off 路径本体 | conversation 区 + open_files 活体渲染 + cache_manifest 区 + `_ring_within_reserve` 及 RECENT CONVERSATION 机器 |
| 2 | kill switch | `AGENT_SESSION_TAPE` 环境变量整个退役;tape 无条件 |
| 3 | off golden | 删除;tape golden 成唯一 |
| 4 | `_TAPE_LAYOUT_SLOTS` | 随三区装配器合并退役 |

## C. Git 版本树纪律

1. **动手前**:`git tag lab-2026-08-05 <HEAD>` ——所有历史 A/B 臂(off/spine/locators)在此 tag 永远可复跑,证据链不断。
2. **每波一分支**:`tape-graduation-w1` / `-w2`;分支内提交序:
   ① 毕业门证据(结果 JSON 入库,单独提交)
   ② 按子系统分提交删除(regions → seed → cli/bench → tests → evals 各一个负 diff 提交,单提交可 revert)
   ③ golden 重钉(单独提交)
   ④ 文档盖戳(单独提交)
3. **合并门**:分支上全量 pytest + ruff 全绿才回 `convergence-p0`;合并后打 `tape-w1-clean` tag。
4. **禁混装**:删除与新功能永不同提交;每个提交信息写明"删什么、为何现在能删、在哪个 tag 可找回"。
5. 工作树时刻 `git status` 干净;跑批产物要么入库要么在 scratchpad,不留未跟踪残渣。

## E. 执行记录(2026-08-05)

- 波一执行于 `tape-graduation-w1`(4 提交,净 −315 行);毕业门 G1/G2a/G2b/G3/G4 绿。
- **波二提前执行**:owner 决策("just proceed wave 2, no need to wait for the test, we will test
  altogether")豁免了"一个发布周期"等待——kill switch 与 off 路径本体、conversation/adjacency/
  cache_manifest 机器、及其测试面一次删除;G2c(s11 活确认)与最终验证合并收口。
- 存活确认:pfc.py 的 conversation 环(tape 的输入基底)、MAX_CONVERSATION/reserve_keep(环修剪)、
  spine 基底(digest 渲染/loader)、locator 渲染器(tape 索引)。

## H. G2c 数据点(s11 typed r5,2026-08-05 晚)

机制:fresh **524.5k(历次最低)**、folds 3、tape 86.7k 有界、同轮 156/156、曲线 92.2%。质量 47/48 + quiz 4/4(log ring 5 rep 3 过,kimi 同探针也挂 = 行为方差)。两项边缘:peak 81.3k(门 80k,+1.6%)、drift 18(带 8–18)。按评审定位为单数据点;终判归 P-T4/P-T5 多 rep 双 checkout。

## G. 波三/波四执行记录(2026-08-05)

- **波三 `ede6a26` 三区装配器**:`region_zone()`(HEAD 冻结 / TAPE 唯一 / TAIL 易变)成为布局第一性;`assembly_slot` 由 zone 推导;`_TAPE_LAYOUT_SLOTS` 退役;`three_zone_partition` 门禁钉分区不变量(恰一 tape、HEAD 必须 REVISION_BOUND、未来区按构造落带下)。三例 fixture 逐字节平价。
- **波四 `07772e7` P4 单车道**:graph 家具裁剪迁入唯一选择器(`regions._graph_trim_selected`,表驱动、门禁监管);`compile_active_context` 纯生产者;第二选择器死输入删除。平价含 graph-active 例。
- 清尾:RESERVE_PAIRS/RESERVE_PRIORITY 孤儿常量、过时注释(render_conversation/adjacency 提及)、探针文档行、session_tape 类型注释全部修正。

## F. 评审响应台账(Task147/Task148,2026-08-05)

两份外部评审 BLOCK,逐项清偿于合并前:
- **T148-b1 重启丢前情**(正常重启铸新 session id → 会话域扫描空):摘要对账改按**任务成员**取档(`load_session_digests(…, None)` + cli 预过滤),最近 reply 经 latest_turns 重冻为 [reply] 条目;跨会话 fixture 断言"指代后续能见先行词"。
- **T147/148 规范门红**:此前只跑 pytest 直跑,漏了仓库规范门 `scripts/run_tests.sh`(独立 CHECKS 套件)。四套件修复(memo_goal 分离修根因、projection kwarg、golden 重钉、AST 探针收窄),现 **188/188**;`git diff --check` 清。
- **T148-h3 请求权威被 task 标签覆写**:R6b 的 memo 键与渲染权威共用变量(spine 时代潜伏)——`memo_goal` 分离,渲染权威恒为 typed current_request。
- **T148-h4 全文渲染算后即弃**:`build_artifacts` 从产品路径摘除(每 build 每文件一次白读);函数保留给 dep-protection/coresidency 机器。
- **T148-f5 残余所有权路径**:session_spine 缓存整链删除(artifact 即唯一摘要库);conversation 三处残余分发、_quoted/_ADJACENCY_ROUNDS、slipstream 目录、_ALWAYS 修净。
- **T148-f6 探针防污染**:spine_probe 拒绝 off/p3/spine 标签(指向 tag),SESSION SPINE 出现即 HARNESS INVALID。
- **提交边界勘误**:波一 `6534070` 实含测试/评估删除(564 行),提交信息的子系统边界描述不准确;波二为 5 个提交非 4;范围按依赖序可回滚、非单提交独立绿。记档为准,不改写已推历史。
- **未清偿(有意,最终验证承担)**:P-T4/P-T5 字节+能力门以 `lab-2026-08-05` 为对照的双 checkout 对比(owner sequencing note:门跑完之前"tape 是架构"是结构决定非测得裁决)。

## D. 执行触发

r8(G1)与 s11 typed r4(G2)数据落地 → 按 A 判门 → 全绿即依 B/C 执行波一。
判门与执行都不再询问,除非任一门红(红则报数据 + 根因分析,等修复决策)。
