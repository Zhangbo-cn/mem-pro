# 03 · 开源项目与框架

- 生成时间：2026-10-14T01:47:48+08:00
- 核验方式：`https://api.github.com/repos/{owner}/{repo}` 实测 ✅。**所有星数、许可证、最近提交日期均为 API 返回的真实值**，原始数据见 `gh_raw.txt`。
- ⚠️ 重要限制：本机 **`github.com` 网页直连超时（HTTP 000）**，`api.github.com` 可达 ✅。因此下表「仓库链接」仅作引用，**未做网页级校验**；星数/license/活跃度以 **API 实测值**为准。
- 活跃度判定：以 `pushed_at`（最近一次推送）为准，基准日 2026-10-14。

---

## 一、总表（按 API 实测）

| # | 仓库 | 用途定位 | 语言 | 许可证 | ⭐ Stars | 最近提交 | 活跃度 |
|---|---|---|---|---|---|---|---|
| 1 | [mem0ai/mem0](https://github.com/mem0ai/mem0) | 通用长期记忆层（提取+更新+检索） | Python | Apache-2.0 | **67,115** | 2026-10-12 | 🟢 高 |
| 2 | [run-llama/llama_index](https://github.com/run-llama/llama_index) | 通用 RAG/数据框架（含 Memory 模块） | Python | MIT | **52,478** | 2026-10-12 | 🟢 高 |
| 3 | [topoteretes/cognee](https://github.com/topoteretes/cognee) | 记忆图谱 + 数据管线 | Python | Apache-2.0 | **32,182** | 2026-10-13 | 🟢 高 |
| 4 | [getzep/graphiti](https://github.com/getzep/graphiti) | 时间知识图谱记忆引擎 | Python | Apache-2.0 | **31,710** | 2026-10-13 | 🟢 高 |
| 5 | [supermemoryai/supermemory](https://github.com/supermemoryai/supermemory) | 记忆 API / 记忆基础设施 | TypeScript | MIT | **31,204** | 2026-10-13 | 🟢 高 |
| 6 | [letta-ai/letta](https://github.com/letta-ai/letta) | 有状态 Agent + 分层记忆（原 MemGPT） | — | Apache-2.0 | **25,119** | 2026-09-10 | 🟡 中 |
| 7 | [NevaMind-AI/memU](https://github.com/NevaMind-AI/memU) | 记忆框架（Agent 记忆管理） | Python | ⚠️NOASSERTION | **14,536** | 2026-10-13 | 🟢 高 |
| 8 | [neuml/txtai](https://github.com/neuml/txtai) | 向量库 + 语义检索/RAG 一体化 | Python | Apache-2.0 | **13,014** | 2026-10-13 | 🟢 高 |
| 9 | [MemTensor/MemOS](https://github.com/MemTensor/MemOS) | 记忆操作系统（记忆调度/生命周期） | TypeScript | Apache-2.0 | **11,850** | 2026-10-10 | 🟢 高 |
| 10 | [getzep/zep](https://github.com/getzep/zep) | 记忆服务（会话记忆 + 图谱） | Python | Apache-2.0 | **4,958** | 2026-10-12 | 🟢 高 |
| 11 | [OSU-NLP-Group/HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG) | 图 + 个性化 PageRank 多跳检索 | Python | MIT | **4,059** | 2026-10-06 | 🟢 高 |
| 12 | [Mirix-AI/MIRIX](https://github.com/Mirix-AI/MIRIX) | 多记忆类型 + 多模态记忆系统 | Python | Apache-2.0 | **3,451** | 2026-10-08 | 🟢 高 |
| 13 | [memodb-io/memobase](https://github.com/memodb-io/memobase) | 用户画像式长期记忆服务 | Python | Apache-2.0 | **2,932** | **2026-01-11** | 🔴 低 |
| 14 | [parthsarthi03/raptor](https://github.com/parthsarthi03/raptor) | 递归摘要树检索 | Python | MIT | **1,768** | **2024-09-03** | 🔴 停滞 |
| 15 | [langchain-ai/langmem](https://github.com/langchain-ai/langmem) | LangChain 记忆原语库 | Python | MIT | **1,700** | 2026-10-02 | 🟢 高 |
| 16 | [agiresearch/A-mem](https://github.com/agiresearch/A-mem) | Agentic Memory（自主记忆组织） | Python | MIT | **1,200** | 2025-12-12 | 🟡 中 |

> 全部 16 个仓库 `archived = false`（未归档）✅。`letta-ai/letta` 的 `language` 字段为 `null`（多语言仓库）；`NevaMind-AI/memU` 的 license 为 `NOASSERTION`（⚠️ 未识别出标准许可证，**商用前必须人工确认**）。

---

## 二、逐项详解（与 AML 接口的适配难度）

适配难度的判定基准：AML 要求 `Add`（**同步**，持久化后可检索才返 200）+ `Search`（`user_id` 隔离、`top_k`≤100、顶层 `data` 数组）两个 HTTP POST，且**不得发送** `metadata`/`app_id`/`agent_id`/`async_mode`。

### 1. mem0ai/mem0 — ⭐67,115 ｜ Apache-2.0 ｜ 适配难度：**低**
- 用途：生产级长期记忆层，提供 add/search 语义，内含事实抽取与更新（对"记忆治理"有内建支持）。
- 可复用点：**最接近 AML Add/Search 语义的开源实现**；其冲突/更新处理直接对应官方 `D 记忆治理`。
- 注意：上游默认流程可能引入异步与 `metadata` 字段，**与 AML 契约冲突，需裁剪**；需自建 `user_id` 隔离与同步返回语义。
- 许可证友好（Apache-2.0），开源方法榜可直接作为底座并披露改动。

### 2. run-llama/llama_index — ⭐52,478 ｜ MIT ｜ 适配难度：**高**
- 用途：通用数据/RAG 框架，Memory 模块与多种索引/检索器齐备。
- 可复用点：组件最全（索引、检索器、重排、后处理），适合作为**零件库**自建。
- 注意：是框架不是服务，需自行封装 Add/Search 服务层；抽象层多，改造量偏大。

### 3. topoteretes/cognee — ⭐32,182 ｜ Apache-2.0 ｜ 适配难度：**中**
- 用途：以图为核心的记忆/数据管线（add → cognify → search）。
- 可复用点：**管线式设计**（add → process → search），与「写入侧多阶段加工」的需求天然契合；图记忆对多跳能力有天然优势。
- 注意：默认管线偏批处理，需改造为同步 Add。

### 4. getzep/graphiti — ⭐31,710 ｜ Apache-2.0 ｜ 适配难度：**中**
- 用途：时间知识图谱记忆引擎（Zep 论文的工程实现），显式建模事实有效期。
- 可复用点：**时间推理 + 记忆失效**（AML `C 时间与事件序列` + `D 记忆治理`）的最佳开源载体；社区极活跃（2026-10-13 推送）。
- 注意：依赖图数据库（Neo4j/FalkorDB 等）与 LLM 抽取，写入延迟与成本较高，需评估 `30 分钟单请求` 与并发。

### 5. supermemoryai/supermemory — ⭐31,204 ｜ MIT ｜ 适配难度：**中**
- 用途：记忆 API / 记忆基础设施（偏产品化，含托管服务形态）。
- 可复用点：接口化程度高，概念与 AML 的"外部记忆服务"接近。
- 注意：TypeScript 生态；若本团队是 Python 栈需跨语言封装。

### 6. letta-ai/letta — ⭐25,119 ｜ Apache-2.0 ｜ 适配难度：**高**
- 用途：有状态 Agent 框架（MemGPT 官方演进），分层记忆 + 自主 I/O。
- 可复用点：分层记忆/上下文管理范式的最佳参考；可直接借用其"记忆分层"设计思想。
- 注意：定位是 **Agent 运行时**而非记忆 API，采用 Agent 循环与工具调用；AML 明确 `不得在 Search 中直接生成最终答案`，需**拆掉 Agent 生成部分**只保留记忆层，改造量大。最近推送 2026-09-10（相对偏慢）。

### 7. NevaMind-AI/memU — ⭐14,536 ｜ ⚠️NOASSERTION ｜ 适配难度：**中**
- 用途：Agent 记忆管理框架。
- 可复用点：活跃（2026-10-13），Python，接口化程度较高。
- **风险**：⚠️ GitHub API 返回 `license = NOASSERTION`，**无法确认许可证**。AML 开源方法榜要求 `GitHub 仓库与固定 Commit` 与开放性披露，**许可证不明会直接影响合规与可复现性**，必须人工确认后再评估。

### 8. neuml/txtai — ⭐13,014 ｜ Apache-2.0 ｜ 适配难度：**低**
- 用途：向量库 + 语义检索 + RAG 的一体化库。
- 可复用点：**同步 add / search 语义天然贴合 AML**（写入即可检索），依赖轻、单机可跑，非常适合 MVP 与 Smoke 快速打通。
- 注意：能力偏"向量检索"，图/多跳/治理需自行补；但对文本赛道 MVP 是性价比最高底座之一。

### 9. MemTensor/MemOS — ⭐11,850 ｜ Apache-2.0 ｜ 适配难度：**中**
- 用途：记忆操作系统（记忆调度、生命周期、记忆立方抽象）。
- 可复用点：MemOS 论文的工程实现，记忆分层与调度抽象对"多能力项综合计分"的文本赛道友好。
- 注意：TypeScript 仓库（API 服务化较完整），需评估与本机 Python 栈的集成成本。

### 10. getzep/zep — ⭐4,958 ｜ Apache-2.0 ｜ 适配难度：**中**
- 用途：Zep 记忆服务端（会话记忆 + 图谱）。
- 可复用点：与 Graphiti 同源，服务化形态更接近 AML；可直接部署为独立服务。
- 注意：需图数据库与额外服务组件，部署复杂度高于单库方案。

### 11. OSU-NLP-Group/HippoRAG — ⭐4,059 ｜ MIT ｜ 适配难度：**高**
- 用途：图索引 + 个性化 PageRank 的**单次调用多跳检索**。
- 可复用点：多跳能力强（对应 AML `B 关系与多跳组合`），且省 LLM 调用。
- 注意：**离线建图**为主，增量写入能力弱；AML 需要"写后立即可检索"，直接套用有结构性冲突，需自行设计增量索引。

### 12. Mirix-AI/MIRIX — ⭐3,451 ｜ Apache-2.0 ｜ 适配难度：**中**
- 用途：多记忆类型 + **多模态**的记忆系统（有服务端）。
- 可复用点：若选**多模态赛道**，这是最直接的开源参考（多记忆类型分支 + 多模态处理）。
- 注意：多智能体带来额外 LLM 调用成本；需核对 FAQ 05 的模型约束。

### 13. memodb-io/memobase — ⭐2,932 ｜ Apache-2.0 ｜ 适配难度：**中**
- 用途：用户画像式长期记忆服务（`user_id` 原生概念）。
- 可复用点：**`user_id` 隔离是其原生设计**，与 AML `user_id 是唯一检索范围` 天然吻合。
- **风险**：🔴 `pushed_at = 2026-01-11`，**近 9 个月无更新**，活跃度低，需评估维护风险。

### 14. parthsarthi03/raptor — ⭐1,768 ｜ MIT ｜ 适配难度：**高**
- 用途：递归摘要树检索。
- **风险**：🔴 `pushed_at = 2024-09-03`，**停滞约 2 年**。仅建议借鉴**方法**（见 `02-papers.md` C1），不建议作为依赖。

### 15. langchain-ai/langmem — ⭐1,700 ｜ MIT ｜ 适配难度：**高**
- 用途：LangChain 的记忆原语（写入/提取/更新策略）。
- 可复用点：记忆"更新/提取"策略的现成模块，可拆用。
- 注意：是库不是服务，且绑定 LangChain 生态。

### 16. agiresearch/A-mem — ⭐1,200 ｜ MIT ｜ 适配难度：**中**
- 用途：Agentic Memory（A-MEM 论文官方实现），自主组织的记忆网络。
- 可复用点：记忆"链接/组织"机制对应多跳与关系能力。
- 注意：研究代码，工程化程度低；`pushed_at = 2025-12-12`，活跃度中等。

---

## 三、选型建议

### 文本赛道（推荐赛道）MVP 底座
- **首选**：`txtai`（同步语义贴合、依赖轻）或 `mem0`（语义最接近、社区最大）。
- **冲榜增强**：叠加 `graphiti`（时间图谱）或 `cognee`（图管线），对应 AML 的 `B/C/D` 能力项。

### 多模态赛道（备选）
- **首选**：`MIRIX`（多记忆类型 + 多模态）或 `memU`（⚠️ 需先确认许可证）。

### 代码赛道（不推荐）
- 无对口开源框架；只能以通用检索 + 仓库解析自建，工程量最大。

### 共同注意
1. **许可证合规**：开源方法榜需公开仓库 + 固定 Commit + 披露改动 ✅；`memU` 的 `NOASSERTION` 必须先人工确认。
2. **必须裁剪**：上述框架的默认字段/异步语义常与 AML 契约冲突（尤其 `metadata`、异步返回），需按官方规范改造为**同步 + `user_id` 隔离 + `top_k` 限流 + 顶层 `data`**。
3. **不要引入重依赖**：本机要求不安装重型依赖、不跑大规模基准（见任务纪律）。选型时优先单机可跑、纯 Python 的方案。
