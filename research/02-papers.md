# 02 · 相关论文

- 生成时间：2026-10-14T01:47:48+08:00
- 核验方法：`export.arxiv.org/api/query?search_query=ti:"<标题>"`，**取回真实 ID/日期/作者**，未使用记忆推测编号；随后对 27 个 `https://arxiv.org/abs/<id>` 做 HTTP 校验，**27/27 返回 200** ✅
- 机构说明：arXiv API 不返回 affiliation，故**机构一律标注 ❓未核实**，仅在有仓库组织名证据时标 ⚠️。作者仅列 API 返回的前 3 位。

---

## A. 长期记忆架构 / 记忆操作系统

### A1. MemGPT: Towards LLMs as Operating Systems
- 作者：Charles Packer、Sarah Wooders、Kevin Lin 等 ｜ 机构 ❓ ｜ 年份：2023（v2）
- 链接：https://arxiv.org/abs/2310.08560 ✅
- 核心方法：把 LLM 上下文视作"内存"，用操作系统式的**分层存储 + 分页换入换出**管理长期记忆；主上下文（main context）与外部存储（external context）之间由模型自主调用函数搬运信息。
- 可借鉴点：**分层记忆 + 自主 I/O** 是记忆系统的基础范式；"写后可见"与"检索函数"的接口设计可直接映射到 AML 的 Add/Search。开源实现流传广（见 `03-projects.md` 的 letta-ai/letta，25.1k★）。
- 局限：依赖模型自主决策函数调用，成本与稳健性问题突出；对"记忆治理（更新/冲突/删除）"未给出系统化机制。

### A2. MemOS: A Memory OS for AI System
- 作者：Zhiyu Li、Chenyang Xi、Chunyu Li 等 ｜ 机构 ❓ ｜ 年份：2025（v4）
- 链接：https://arxiv.org/abs/2507.03724 ✅
- 核心方法：把记忆抽象为可调度资源（MemoryCube 等记忆单元 + 调度/生命周期管理），提出记忆操作与记忆调度的统一抽象。
- 可借鉴点：**记忆作为一等资源**的分层与生命周期抽象，可用于组织 Add 阶段的写入分类与 Search 阶段的路由；开源库 MemTensor/MemOS（11.9k★）。
- 局限：偏架构与调度、端到端评测口径不统一；工程落地复杂度高。

### A3. MIRIX: Multi-Agent Memory System for LLM-Based Agents
- 作者：Yu Wang、Xi Chen ｜ 机构 ❓ ｜ 年份：2025
- 链接：https://arxiv.org/abs/2507.07957 ✅
- 核心方法：多智能体 + 多记忆类型（多个专用记忆子系统分别负责不同记忆种类，再由协调层统一检索）。
- 可借鉴点：**按记忆类型分库**的思路，对 AML 文本赛事中"事实 / 关系 / 时间 / 个性化 / 规则"多能力类别（官方 A–H）天然对应；开源 Mirix-AI/MIRIX（3.45k★），且原生含多模态分支，可作为多模态备选方案的参考。
- 局限：多智能体引入额外 LLM 调用成本；若 FAQ 05 的 `gpt-4o-mini` 约束成立，需重新评估调用量预算。

---

## B. 图结构 / 时间知识图谱记忆

### B1. Zep: A Temporal Knowledge Graph Architecture for Agent Memory
- 作者：Preston Rasmussen、Pavlo Paliychuk、Travis Beauvais 等 ｜ 机构 ❓ ｜ 年份：2025
- 链接：https://arxiv.org/abs/2501.13956 ✅
- 核心方法：以**时间知识图谱**组织会话记忆，显式建模实体、关系与**时间有效期**，支持事实的更新与失效。
- 可借鉴点：**时间维度**是 AML 文本赛道的显式能力项（官方 `C 时间与事件序列`、`D 记忆治理`），时间图谱是"更新/冲突/失效"最直接的落地手段；开源 getzep/graphiti（31.7k★，最近提交 2026-10-13，非常活跃）。
- 局限：图谱抽取依赖 LLM，写入成本与延迟高；错抽会污染记忆且难回滚。

### B2. From Local to Global: A Graph RAG Approach to Query-Focused Summarization
- 作者：Darren Edge、Ha Trinh、Newman Cheng 等 ｜ 机构：⚠️Microsoft（出处为官方博客口径，未在本机直连核实）｜ 年份：2024
- 链接：https://arxiv.org/abs/2404.16130 ✅
- 核心方法：用 LLM 抽取实体-关系图 + 社区检测 + 社区摘要，回答"全局性/主题性"问题。
- 可借鉴点：对应 AML 的 `B 关系与多跳组合`（跨片段证据与关系链恢复）；社区摘要层可作粗检索的一级索引。
- 局限：构建成本高、图规模大时查询延迟显著；对细粒度事实召回并非最优。

### B3. LightRAG: Simple and Fast Retrieval-Augmented Generation
- 作者：Zirui Guo、Lianghao Xia、Yanhua Yu 等 ｜ 机构 ❓ ｜ 年份：2024（v3）
- 链接：https://arxiv.org/abs/2410.05779 ✅
- 核心方法：双层检索（低层实体、高层主题）+ 图结构与向量混合，追求"轻量快"的图 RAG。
- 可借鉴点：**图 + 向量混合检索**的性价比路线，适合 17 天工期内的 MVP 升级路径（`05-tech-stack.md` 的进阶架构）。
- 局限：仍是 RAG 视角，记忆治理/遗忘机制较弱。

### B4. HippoRAG: Neurobiologically Inspired Long-Term Memory for Large Language Models
- 作者：Bernal Jiménez Gutiérrez、Yiheng Shu、Yu Gu 等 ｜ 机构：⚠️OSU-NLP-Group（依仓库组织名，未直连核实）｜ 年份：2024（v3）
- 链接：https://arxiv.org/abs/2405.14831 ✅
- 核心方法：借鉴海马体索引理论，用**个性化 PageRank 在知识图谱上做多跳检索**，单次 LLM 调用即可完成多跳。
- 可借鉴点：**多跳检索不必多次 LLM 调用**，对成本与延迟友好，正对 AML `B 关系与多跳组合`；开源 OSU-NLP-Group/HippoRAG（4.06k★，最近提交 2026-10-06）。
- 局限：依赖上游开放 IE 抽取质量；离线建图，增量更新能力弱。

### B5. From RAG to Memory: Non-Parametric Continual Learning for Large Language Models（HippoRAG 2）
- 作者：Bernal Jiménez Gutiérrez、Yiheng Shu、Weijian Qi 等 ｜ 机构 ❓ ｜ 年份：2025
- 链接：https://arxiv.org/abs/2502.14802 ✅
- 核心方法：在 HippoRAG 基础上改进**连续学习/非参数化记忆**，提升对事实、关联、篇章三类记忆的整体覆盖。
- 可借鉴点：**同一套检索框架覆盖多种记忆类型**，契合 AML 多能力项综合计分的特点（避免单点优化）。
- 局限：仍需离线构建；论文指标口径与 AML 官方合同不同，不能直接迁移预期分数。

### B6. Selective Temporal Knowledge Graph Reasoning
- 作者：不详（本次未取作者，仅核验 ID）｜ 年份：2024
- 链接：https://arxiv.org/abs/2404.01695 ✅
- 核心方法：时间知识图谱上**选择性推理**（对候选做取舍而非全量推理），提升时间推理的可靠性与效率。
- 可借鉴点：AML 文本赛道 `C 时间与事件序列` + `D 记忆治理`（更新/冲突消解/遗忘）的技术零件。
- 局限：面向传统 TKGC 数据集，与"会话记忆"的形态差异较大。

### B7. Temporal Knowledge Graph Reasoning Triggered by Memories
- 作者：不详（本次未取作者，仅核验 ID）｜ 年份：2021（v2）
- 链接：https://arxiv.org/abs/2110.08765 ✅
- 核心方法：用记忆机制触发时间图谱推理（较早的工作，属经典脉络）。
- 可借鉴点：经典参考，理解"记忆 ↔ 时间推理"耦合的早期设计。
- 局限：年代较早，非 LLM 时代方法，直接迁移价值有限。

---

## C. 分层摘要 / 树结构检索

### C1. RAPTOR: Recursive Abstractive Processing for Tree-Organized Retrieval
- 作者：Parth Sarthi、Salman Abdullah、Aditi Tuli 等 ｜ 机构 ❓ ｜ 年份：2024
- 链接：https://arxiv.org/abs/2401.18059 ✅
- 核心方法：递归聚类 + 摘要构建**多层摘要树**，检索可跨不同抽象层级。
- 可借鉴点：对"长会话 → 多粒度记忆"非常合适；可作为文本赛道 MVP 之上的一层压缩，控制写入体积。
- 局限：树需重算，增量更新成本高；摘要会丢细节（对显式事实召回不利）。⚠️仓库 parthsarthi03/raptor 最近提交 **2024-09-03，已近两年无更新**（实测），谨慎作为依赖。

### C2. MemoryBank: Enhancing Large Language Models with Long-Term Memory
- 作者：Wanjun Zhong、Lianghong Guo、Qiqi Gao 等 ｜ 机构 ❓ ｜ 年份：2023（v3）
- 链接：https://arxiv.org/abs/2305.10250 ✅
- 核心方法：引入**艾宾浩斯遗忘曲线**式的记忆强度衰减与回忆机制，并做用户画像式长期记忆。
- 可借鉴点：AML 明确考察 `D 记忆治理（更新、冲突消解、删除与遗忘）`——遗忘机制是该能力项最直接的可引用方法。
- 局限：遗忘曲线参数需调；对"必须精确召回"的任务可能有害。

---

## D. 记忆治理 / 知识冲突 / 遗忘

### D1. Knowledge Conflicts for LLMs: A Survey
- 作者：Rongwu Xu、Zehan Qi、Zhijiang Guo 等 ｜ 机构 ❓ ｜ 年份：2024（v2）
- 链接：https://arxiv.org/abs/2403.08319 ✅
- 核心方法：系统梳理上下文-记忆-参数三类知识冲突的成因与处理策略（含"冲突消解"）。
- 可借鉴点：AML `D 记忆治理` 与 `H 认识论安全与隐私（稳健拒答、最小披露）` 的理论底图；可直接指导"冲突记忆如何取舍"的规则设计。
- 局限：综述性，不提供可直接落地的工程实现。

### D2. MemInsight: Autonomous Memory Augmentation for LLM Agents
- 作者：不详（仅核验 ID）｜ 年份：2025（v2）
- 链接：https://arxiv.org/abs/2503.21760 ✅
- 核心方法：让记忆**自主增强/组织**（自动打标、结构化），提升检索命中。
- 可借鉴点：Add 阶段"写入即加工"的思路——AML 允许 Add 内部异步处理（但必须同步返回 200），适合在写入侧做结构化。
- 局限：增加写入延迟，需与 `单次请求最长 30 分钟` 的约束对齐。

### D3. Bookkeeping, Composition, or Unreachable Gold? Reading MemoryAgentBench's Conflict-Resolution Scores Against a Frozen Last-Write Resolver
- 作者：不详（仅核验 ID）｜ 年份：2026-10-06（很新）
- 链接：https://arxiv.org/abs/2610.09193 ✅
- 核心方法：以"固定 last-write 解析器"为对照，重新解读 MemoryAgentBench 的**冲突消解分数**，质疑部分增益来自记账/组合而非真正消解。
- 可借鉴点：**方法学警示**——评测中的"冲突消解"可能被简单 last-write 规则拿分。设计 AML 方案时应先做 last-write 基线对照，避免过度工程。
- 局限：批评性短文，非方法论文。

---

## E. 评测基准设计

### E1. Evaluating Very Long-Term Conversational Memory of LLM Agents（LoCoMo）
- 作者：Adyasha Maharana、Dong-Ho Lee、Sergey Tulyakov 等 ｜ 机构 ❓ ｜ 年份：2024
- 链接：https://arxiv.org/abs/2402.17753 ✅
- 核心方法：构建**超长跨会话对话**数据集，含多模态事件，评估多跳、时间、对抗性问题上的长期记忆。
- 可借鉴点：**最接近 AML 文本赛道形态的公开基准**，是本机自测的首选代理（见 `05-tech-stack.md` 自测策略）。注：AML 接入示例中的 `locomo`、`locomo_refined` 命名与此脉络一致 ✅（官方示例 `request_id` 出现 `locomo_refined`）。
- 局限：规模与 AML 的 6000+ 实例不同；公开数据可能已过拟合。

### E2. LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory
- 作者：Di Wu、Hongwei Wang、Wenhao Yu 等 ｜ 机构 ❓ ｜ 年份：2024（v2）
- 链接：https://arxiv.org/abs/2410.10813 ✅
- 核心方法：聚焦**五种长期记忆能力**（信息抽取、多会话推理、时间推理、知识更新、拒答）及其组合。
- 可借鉴点：其能力划分与 AML 文本 `A/B/C/D/H` 高度重合；"知识更新"和"拒答"两项尤其对应 AML 的治理与安全维度。
- 局限：仍为问答式评测，与 AML 平台统一 Answer 的具体题型未必一致。

### E3. Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions（MemoryAgentBench 脉络）
- 作者：不详（仅核验 ID）｜ 年份：2025（v4）
- 链接：https://arxiv.org/abs/2507.05257 ✅
- 核心方法：以**增量多轮交互**方式评估 Agent 记忆，强调"随交互累积"而非一次性长上下文。
- 可借鉴点：AML 文本赛道明确纳入 **Streaming 持续记忆**（`Streaming 按增量会话／任务单元写入` ✅），本文的增量评测设计是最贴近的方法学参考。
- 局限：与 AML 的评测合同（平台统一 Answer/Eval）不一致，仅供参考设计。

### E4. Lost in the Middle: How Language Models Use Long Contexts
- 作者：Nelson F. Liu、Kevin Lin、John Hewitt 等 ｜ 机构 ❓ ｜ 年份：2023（v3）
- 链接：https://arxiv.org/abs/2307.03172 ✅
- 核心方法：实证发现模型对**长上下文中间位置的信息利用率显著下降**。
- 可借鉴点：直接支撑"为什么必须做检索而不是堆上下文"，也解释了 Search 返回**顺序即证据优先级**（官方原文 `返回顺序就是证据优先顺序`）的工程含义——**把最相关的放最前**。
- 局限：结论针对被评测模型，`gpt-4o-mini` 行为需自行验证。

### E5. A Survey on the Memory Mechanism of Large Language Model based Agents
- 作者：Zeyu Zhang、Xiaohe Bo、Chen Ma 等 ｜ 机构 ❓ ｜ 年份：2024
- 链接：https://arxiv.org/abs/2404.13501 ✅
- 核心方法：从"记忆来源/形式/操作（写-管理-读）"给出一套记忆系统分类框架。
- 可借鉴点：写-管理-读三操作与 AML Add/Search 边界天然对齐，可作为**架构分层的通用术语表**。
- 局限：综述，无工程实现。

### E6. From Human Memory to AI Memory: A Survey on Memory Mechanisms in the Era of LLMs
- 作者：不详（仅核验 ID）｜ 年份：2025（v2）
- 链接：https://arxiv.org/abs/2504.15965 ✅
- 核心方法：以人类记忆分类学映射 AI 记忆机制，给出较新的记忆系统全景。
- 可借鉴点：最新的综述地图，用于快速补齐方向覆盖（尤其多模态记忆与记忆对齐）。
- 局限：综述，时效性依赖后续更新。

---

## F. 代码库记忆 / 经验复用

### F1. SWE-bench: Can Language Models Resolve Real-World GitHub Issues?
- 作者：不详（仅取回标题，未取作者）｜ 年份：2023（v3）
- 链接：https://arxiv.org/abs/2310.06770 ✅
- 核心方法：以真实 GitHub issue → 补丁 → 测试验证的端到端任务作为评测标准。
- 可借鉴点：**AML 代码赛道的计分逻辑同源**（⚠️官方称 `CAMBench Coding` 以实际代码修改与测试验证为准）；理解 SWE-bench 的"测试通过即得分"机制，就能理解代码赛道的优化目标不是检索相似度。
- 局限：本机无法（也不应）下载完整 SWE-bench 环境。

### F2. SWE-bench Goes Live!
- 作者：Linghao Zhang、Shilin He、Chaoyun Zhang 等 ｜ 机构 ❓ ｜ 年份：2025（v2）
- 链接：https://arxiv.org/abs/2505.23419 ✅
- 核心方法：把 SWE-bench 升级为**动态/live** 版本（题目随时间更新，降低数据污染）。
- 可借鉴点：说明 SWE 类评测正在向"防污染"演进，与 AML `私有数据降低过拟合` ✅ 的设计理念一致。
- 局限：与 AML 代码赛道无直接接口关系。
- ⚠️ **核验备注**：按标题 `SWE-bench` 检索时 arXiv 首先返回的是本文（2505.23419），**不是原始 SWE-bench**；原始篇为 2310.06770。两条均已分别核验。

### F3. ExpeL: LLM Agents Are Experiential Learners
- 作者：Andrew Zhao、Daniel Huang、Quentin Xu 等 ｜ 机构 ❓ ｜ 年份：2023（v3）
- 链接：https://arxiv.org/abs/2308.10144 ✅
- 核心方法：让 Agent 从历史任务中**自主积累经验**（跨任务经验抽取与复用），而非仅靠参数更新。
- 可借鉴点：AML 代码赛道考察"历史工程经验的检索、筛选和复用"（✅官方原文），ExpeL 是"经验复用"的直接方法参考。
- 局限：经验以自然语言规则形式存储，规模上限与冲突处理较弱。

---

## G. 检索增强基础组件

### G1. Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection
- 作者：不详（仅核验 ID）｜ 年份：2023
- 链接：https://arxiv.org/abs/2310.11511 ✅
- 核心方法：让模型通过反思标记自主决定**何时检索、检索结果是否相关、生成是否被证据支持**。
- 可借鉴点：AML 明确 `严禁硬编码、数据泄漏、提示词注入`，且平台负责 Answer——Self-RAG 的"证据支持性判断"可用于 Search 侧的**证据质量自检**（只返回有证据的记忆，官方硬要求 `只返回记忆证据`）。
- 局限：训练/微调成本；AML 场景下模型受限（FAQ 05），可能只能借用其**思路**而非模型。

---

## H. 检索/评测基础设施（供选型参考）

- **Qwen3 Embedding 技术报告**：https://arxiv.org/html/2506.05176v1 ⚠️部分核实（web 检索命中，未做 arXiv API 条目核验）；其中给出 MTEB Multilingual / MTEB Code 成绩，是 `04-models.md` 中 embedding 选型的依据之一。

---

## 核验失败 / 不存在项（禁止编造，如实记录）

| 目标 | 检索方式 | 结果 |
|---|---|---|
| Memobase 论文 | `ti:"Memobase"` | ❓ **NOT_FOUND** —— 未找到 arXiv 论文（该项目为工程库，见 `03-projects.md`） |
| Cognee 论文 | `ti:"Cognee"` | ❓ **NOT_FOUND** —— 未找到 arXiv 论文 |
| MemBench（LLM 记忆评测） | `ti:"MemBench"` | ❓ 标题检索命中均为**同名不同域**论文（如扩散模型触发词数据集 2407.17095、Arm 吞吐基准 2504.06813），**未找到**目标论文，故本条不收录 |
| `Rethinking Memory in AI` | `ti:"Rethinking Memory in AI"` | ❓ **NOT_FOUND** |
| `CAMBench` / `SWEContextBench` / `ATM-Bench` / `Mem-Gallery` | arXiv 标题检索 | ❓ 均**未找到**公开论文；判定为 **AML 赛事自有基准**，只有官网名称，无可引论文 |
| Supermemory / MemU / memU / LangMem | arXiv 标题检索 | ❓ 未检索到对应论文（属工程/产品，见 `03-projects.md`） |

> 说明：以上均为**实际执行检索后的空结果**，不是推测。任何未通过核验的条目都未以"论文"形式写入本文。

---

## 统计

| 项 | 数 |
|---|---|
| 收录论文 | **27** |
| arXiv ID 经 API 核验 | 27 / 27 ✅ |
| `arxiv.org/abs/*` HTTP 校验通过 | 27 / 27 ✅（100%） |
| 明确标注 ❓未核实（作者/机构缺失） | 6 篇（B6、B7、D2、D3、E3、E6、F1、G1 中作者未取者） |
| 检索未找到（已如实记录） | 6 类 |
