# 提交材料（Agent Memory Challenge 2026 第二期 · 多模态记忆赛道 · 开源方法榜）

> 对应 `research/06-open-method-track-checklist.md` 的 15 项核对表。
> 所有「☐」由提交人现场填写；标 ✅ 的部分已由代码与实测支撑，可直接引用。
>
> **说明**：这是**报名表要申报的信息**，不是评测内容。评测题面（Add 的消息、Search 的 query、
> Answer 判分）由平台驱动，参赛方看不到也不用提交——我们只提供 Add / Search 两个 HTTP 接口。
> 「待填」= 只有提交人手上有、代码里无法自动生成的四类信息：联系人/机构、可公开展示范围、
> 部署后的接口地址、冻结后的 commit 与 config_hash。

---

## 一、基本信息

| 项 | 内容 |
|---|---|
| 系统名称 | **mem-pro** |
| 版本号 | `mem-pro/0.2.0`（Full 受理后冻结，不得更换） |
| 固定 Commit | `6d2147e9a1a88a344461b20b374a81c4d200d547`（tag `v1.0.0`；**v1.0.0 冻结时**线上 `/version` 的 `commit` = `6d2147e9a1a8`，`config_hash` = `92d381b0beb88e5a`；当前线上已升到 v2.0，见 §九） |
| 配置指纹 | `92d381b0beb88e5a`（v1.0.0 冻结值，16 位；改任何排序参数或 `MAX_IMAGES` / `MAX_RESULTS` / `IMG_MAX_SIDE` / `MEMORY_UNIT` / `CONTENT_HEADER` 都会变；v2.0 为 `8b8e31d7bacbafe8`） |
| 公开仓库 | https://github.com/Zhangbo-cn/mem-pro （**公开**，MIT 许可；仓库内无任何凭据；评测版本以 tag `v1.0.0` 为准） |
| 联系人 / 团队 | **张播（个人参赛）**；电话/邮箱与报名表一致（不写入仓库，避免仓库扩散联系信息） |
| 赛道 / 组别 | 多模态记忆赛道 / **开源方法榜**（组别只能选一个） |
| 接口地址 | **`https://113.44.133.30/add`**、**`https://113.44.133.30/search`**（IP 直签 Let's Encrypt，无需域名/备案；`http://` 308 跳 https，Caddy 需 `default_sni`，见 `deploy/REMOTE_OPS.md` §1.4.1） |
| 鉴权方式 | `Authorization: Token <Memory System Key>`（同时接受 `Bearer` / `X-Api-Key`） |
| 允许公开展示 | ✅ ① 系统名 + 方法说明 + 分类指标；② 架构图；③ 代码仓库（公开）。**不公开**：Memory System Key、评测数据样本、原始 query |

---

## 二、方法说明

### 2.1 架构

单进程 Python 标准库服务（numpy 仅作可选加速），SQLite 单文件持久化，**零第三方依赖**。
Add 与 Search 全链路不超过一次外部调用（向量化），**检索路径零 LLM**。

```
POST /add    messages ──▶ 规范化 → 分块(≤1 条消息/记忆) → 去重(text+图片指纹) → embedding → SQLite
POST /search query ──▶ embedding ──┬─▶ 稠密余弦(归一化向量点积)
                                   └─▶ BM25 稀疏(saturation 归一化)
                        ──▶ 线性融合(α=0.95/β=0.05) ──▶ 时态规则重排 ──▶ 近似去重
                        ──▶ [可选] 交叉编码器精排 top-10 ──▶ 证据预算裁剪 ──▶ 返回
```

### 2.2 Add 侧（写入）

| 环节 | 做法 | 目的 |
|---|---|---|
| 规范化 | content 支持纯文本或有序 `[{type:text},{type:image_url}]`；时间戳统一毫秒 | 保住图文的原始顺序与时间 |
| 分块 | 一条消息 = 一条记忆，不跨轮合并 | 证据粒度与题面 `D1:2` 级 clue 对齐 |
| 去重 | `sha256(text) + 图片内容指纹`；纯图记忆不因文本相同被误删 | 抑制重复投递，同时不丢不同图片 |
| 幂等 | `(user_id, request_id)` 唯一，重复请求返回首次结果 | 平台重试 32 次不产生重复记忆 |
| 向量 | `text-embedding-v4`，1024 维；图片消息的 caption 拼入文本 | 单一通道同时覆盖图文 |
| 失败语义 | 空 message 列表写入空结果；上游失败整体回滚不部分写入 | 重试不会产生半条记忆 |

### 2.3 Search 侧（检索）

| 环节 | 做法 | 依据 |
|---|---|---|
| 稠密 | 归一化向量内积（cosine），float64 全程，bit-identical | 确定性 |
| 稀疏 | BM25 + `saturation` 归一化 + 稀有词加权（df < 15%） | 专有名词、编号、日期靠稀疏 |
| 融合 | 线性加权，α=0.95 / β=0.05；RRF 为可选模式 | 在 Mem-Gallery 上 a/b 调参选定 |
| 时态 | 查询含「现在/最新/currently」时，正文只陈述旧事实（「去年/以前/used to」）的记忆扣 0.06；含「以前/上次」时反向加分。只读正文措辞、不看时间戳 | 更新类问题（旧事实被新事实替代） |
| 去重 | 头部 200 条内正文指纹相同或向量余弦 ≥ 0.97 视为近重复，保留排名靠前者；图片不同的纯图记忆不去重 | 减少多轮重复表达挤占名额 |
| 精排 | **本次提交启用**（`RERANK_MODE=http`，`RERANK_KIND=dashscope`，`RERANK_MODEL=qwen3-rerank`，`RERANK_TOP=30`、`RERANK_BLEND=0.7`）；对前 30 条候选精排，再与融合名次做 RRF 混合（`1/(k+rank)`，w=0.7），最后把分数单调映射回融合区间；**分差 < 0.0003 视为并列**，按融合分/时间/ID 定序。代码默认值仍为 `RERANK_MODE=none`（无精排服务也能跑） | 参营决定（2026-10-27）；代理榜 MRR 0.761→0.808（top30+blend 后 0.805/0.805，R@10 +0.034/+0.040，见 README §十五），端到端答案评测无可靠收益（±0.02，n=400）→ 质量优先；容差处理上游 1e-4 级分数抖动，连续失败熔断并降级 |
| 裁剪 | 返回条数 ≤ 平台 top_k（`MAX_RESULTS=0` 不另行收窄）；单响应 token ≤ 109,516、字节 ≤ 28 MiB、图片 ≤ 3 张（`MAX_IMAGES=3`，代码默认 6，本次按 2 Mbit 带宽收紧）；返回图按 `IMG_MAX_SIDE=1024` 降采样（仅缩尺寸，不裁剪内容） | 平台的 Answer 侧预算；2 Mbit 出网下把平均响应从 2.74 MiB 压到 0.32 MiB |

### 2.4 多模态披露（官方要求明示）

| 问题 | 回答 |
|---|---|
| 是否使用**原图** | **是**。原图原样入库并在命中时作为 `image_url` 内联返回；图片参与去重指纹 |
| 是否使用 **caption** | 平台下发的 caption **原样参与检索文本**（不改写、不二次生成）。系统内置可选 `CAPTION_MODE=openai`，**本期评测保持 `none`**，即不额外调用任何视觉模型 |
| 是否使用 **OCR** | **否** |
| 是否**返回图片证据** | **是**，单响应最多 3 张（`MAX_IMAGES=3`），超出按融合分优先保留；长边超过 1024 的图会降采样后再内联返回 |

---

## 三、原始工作引用与方法改动说明

### 3.1 引用

| 原始工作 | 用在哪 | 引用 |
|---|---|---|
| BM25 | 稀疏通道打分与饱和归一化 | Robertson & Zaragoza, *The Probabilistic Relevance Framework: BM25 and Beyond*, FnTIR 2009 |
| RRF | 可选融合模式（默认关闭，用线性融合） | Cormack et al., *Reciprocal Rank Fusion*, SIGIR 2009 |
| 交叉编码器精排 | 本次提交启用（top-30 候选重排 + RRF 混合 `blend=0.7`，失败自动降级） | 模型服务：阿里云百炼 `qwen3-rerank`（`gte-rerank-v2` 官方标注 2026-05-30 下线） |
| 文本向量 | 稠密通道 | 阿里云百炼 `text-embedding-v4`（Qwen3-Embedding 系列） |
| 近重复抑制 | 检索结果去重 | 余弦阈值法，本仓库自实现 |
| Mem-Gallery（本地代理榜数据） | `data/mem-gallery/`，`tools/gallery_proxy.py` / `e2e_proxy.py` | Bei et al., arXiv:2601.03515；数据 MIT（`Ethan-Bei/Mem-Gallery`） |
| ATM-Bench（本地代理榜数据） | `data/atm-bench/`、`data/raw_memory/`，`tools/atm_proxy.py` | Mei et al., *According to Me*, arXiv:2603.01990；数据 **CC BY-NC 4.0**（`Jingbiao/ATM-Bench`），见 `data/README.md` |

### 3.2 改动说明

本仓库**全部代码自研**（`service/` 约 2.7k 行、`tools/` 约 3.5k 行、测试 101 项），未复用任何论文官方实现或第三方框架代码，因此无「相对原始工作的代码级改动」需披露。参考的仅为上述通用算法思想，实现细节（融合权重、时态线索匹配、去重阈值、证据预算、确定性处理）均为本团队在公开数据集 Mem-Gallery 与 ATM-Bench 上实测调出的。

---

## 四、容量与运行限制声明

| 项 | 值 |
|---|---|
| 部署形态 | 单机 Docker（Python 3.12-slim + numpy），1 容器 + 可选 Caddy 反代 |
| 资源 | 2 vCPU / 2 GiB / 40 GiB 起（实测 29 req/s 时 CPU 0.52 核、RSS 242 MiB） |
| **申报并发（上界）** | Search **2** / Add **2**（保守口径：公网 2 Mbit 下 `tools/rehearse.py` 实测 3 并发吞吐 0.243 req/s / p95 7.9 s，本次按 60–70% 取 2；回环压测 8 并发 39 req/s、4 并发 p95 447 ms 不作为申报依据——瓶颈在 EIP 出网带宽） |
| 平均延迟 | 公网实测（真图）：Search p50 ≈ 1.5 s、p95 ≈ 3.8 s（响应 p50 0.34 MiB，缩图后，含 top-30 精排）；回环 16 并发 p50 ≈ 0.21 s |
| 单请求超时 | 服务端不主动断连；平台 30 分钟上限内均可用 |
| 限流 | 在途请求上限 64（超出返回 `429` + `Retry-After`）；上游配额不足时内部重试，重试预算 12 s，耗尽返回 `503` + `Retry-After: 5` |
| 数据规模 | 单 user 记忆量线性扩展，1 万条记忆时检索 p50 仍在百毫秒级 |
| 幂等 | 相同 `(user_id, request_id)` 重放返回首次结果，字节一致 |

---

## 五、运行与复现说明

```bash
git clone https://github.com/Zhangbo-cn/mem-pro && cd mem-pro
cp service/.env.example .env      # 填 MEMORY_SYSTEM_KEY / DASHSCOPE_API_KEY
docker compose --profile tls up -d --build
curl -s -H "Authorization: Token $KEY" http://113.44.133.30/version  # commit 与 config_hash 必须与提交材料一致
```

- 复现性：`EMBED_MODE=api`、`text-embedding-v4`、`temperature=0`（caption 关闭时无采样）；打分全程 float64；并列按 `融合分 ↓ → created_at ↓ → id ↑` 定序；`tools/replay.py` 验证 5 次重放字节一致。
- 失效降级：精排连续失败 3 次进入 60 s 冷却并降级到融合序，`/version` 的 `rerank.degraded` 会置位（本期评测前已用容差+名次映射消除上游分抖动，正常情况下不应触发）。
- 依赖：Python ≥ 3.10，numpy 可选；无网络依赖除 `text-embedding-v4`（及精排）API。

---

## 六、数据处理与诚信承诺

| 项 | 承诺 |
|---|---|
| 数据用途 | 评测数据**仅**用于完成本次评测任务；不用于训练、微调、产品分析、数据集重建或对外传播 |
| 日志 | 仅记录元数据（路径、状态码、耗时、`user_id` 的 sha1 前 12 位、Request-Id），**不落请求正文与 query 原文**；有自动化测试守护 |
| 保留期 | 任务完成后 **30 天内删除**；提供 `tools/purge.py` 按 user 前缀删除并 VACUUM |
| 样本隔离 | 检索严格按 `user_id` 过滤，跨 user、跨任务不共享记忆 |
| 只返回证据 | Search 只返回记忆条目，不生成或改写答案 |
| 原创性 | 代码为本团队自研，引用与借鉴已在 §三 全部披露 |
| 不作弊 | 无硬编码答案、无数据泄漏、无提示词注入、无人工介入答题、无结果操纵 |

---

## 七、15 项核对表状态

| # | 材料 | 状态 |
|---|---|---|
| 1 | 系统名称与版本号 | ✅ `mem-pro/0.2.0` |
| 2 | 联系人 / 机构 | ✅ 张播（个人）；联系方式见报名表 |
| 3 | 赛道 + 组别 | ✅ 多模态 / 开源方法榜 |
| 4 | 方法说明 + 引用 | ✅ §二、§三 |
| 5 | 在线 Add / Search（HTTPS 公网） | ✅ `https://113.44.133.30/add`、`https://113.44.133.30/search`（Let's Encrypt IP 证书，`commit` 见 `/version`、`config_hash=92d381b0beb88e5a`（v1.0.0 冻结值，含 `IMG_MAX_SIDE=1024` 缩图；当前线上为 v2.0 的 `8b8e31d7bacbafe8`，见 §九）；contract 27/27、smoke 18/18、replay 幂等+5 次字节一致均走 https 复验） |
| 6 | 鉴权方式声明 | ✅ §一 |
| 7 | 容量与运行限制 | ✅ §四 |
| 8 | GitHub 仓库 | ✅ 公开仓库（MIT），代码无凭据 |
| 9 | 固定 Commit | ✅ `6d2147e9a1a8…`（tag `v1.0.0`），线上 `/version` 一致 |
| 10 | 原始工作引用 | ✅ §3.1 |
| 11 | 方法改动说明 | ✅ §3.2 |
| 12 | 可公开展示的信息范围 | ✅ 方法说明 / 分类指标 / 架构图 / 代码仓库（公开）；不含 Key、数据样本、原始 query |
| 13 | 依赖与运行说明 | ✅ §五 |
| 14 | 原创性披露 + 诚信承诺 | ✅ §六 |
| 15 | Memory System Key | ✅ 随报名表私有字段提交（48 位 hex，仅存服务器 `.env`，**不入仓库**） |

---

## 八、报名表可直接粘贴（2026-10-28 定稿）

### 8.1 报名表字段速填

| 表单字段 | 直接填这个 |
|---|---|
| 系统名称 | `mem-pro` |
| 版本号 | `mem-pro/0.2.0` |
| 赛道 / 组别 | 多模态记忆 / 开源方法榜 |
| 联系人 | 张播 |
| 电话 / 邮箱 | 只填在报名表里（不写入仓库） |
| 机构 / 团队 | 个人 |
| Add 地址 | `https://113.44.133.30/add` |
| Search 地址 | `https://113.44.133.30/search` |
| 鉴权头 | `Authorization: Token <Memory System Key>`（48 位 hex，随本表私有字段提交） |
| 固定 Commit | `6d2147e9a1a88a344461b20b374a81c4d200d547`（tag `v1.0.0`） |
| 配置指纹 | `92d381b0beb88e5a`（v1.0.0 冻结值；v2.0 见 §九） |
| 申报并发 | Search **2** / Add **2** |
| 仓库 | `https://github.com/Zhangbo-cn/mem-pro`（公开，MIT） |
| 可公开展示 | 方法说明、分类指标、架构图、代码仓库；不公开 Key、评测数据样本、原始 query |

### 8.2 系统简介（中文，约 120 字，可直接粘）

> mem-pro 是一个零第三方依赖的 Add / Search 记忆服务：写入侧按消息粒度保序落库、以 `text-embedding-v4`（1024 维）建向量并做内容指纹去重与 `(user_id, request_id)` 幂等；检索侧用 dense（cosine）+ BM25 线性融合（α=0.95/β=0.05）、时态一致性规则（问「现在」时压低纯旧事实）、近重复去重，再对前 30 条候选做 `qwen3-rerank` 精排并与名次做 RRF 混合；返回前按 token / 字节 / 图片三重预算裁剪并对返回图降采样。全链路确定性：同 query 连续多次响应字节一致。

### 8.3 System summary (English, paste-ready)

> mem-pro is a dependency-free Add/Search memory service. Writes are stored in message order with `text-embedding-v4` (1024-d) vectors, content-fingerprint de-duplication and `(user_id, request_id)` idempotency. Search fuses dense cosine with BM25 (α=0.95, β=0.05), applies a tense-consistency rule (demotes stale-only facts when the query asks "now"), removes near-duplicates, then reranks the top-30 candidates with `qwen3-rerank` blended into the fused ranking via RRF. Responses are clipped by a triple budget (tokens / bytes / images) and oversized images are downsampled. End-to-end deterministic: repeated identical queries return byte-identical responses.

### 8.4 合规与限制声明（可直接粘）

- Embedding：`text-embedding-v4`（官方指定）。
- LLM：本期**未调用**任何生成式模型（`CAPTION_MODE=none`）；代码内置的可选 caption 仅支持 `gpt-4o-mini`。
- Reranker：`qwen3-rerank`（官方明确 Reranker 不受模型限制）。
- 单请求：服务端不主动断连，平台 30 分钟上限内均可用；在途超 64 返回 `429`+`Retry-After`。
- 数据：评测数据仅用于完成本次任务，不训练、不外传；任务结束 30 天内用 `tools/purge.py` 删除。
- 可复现：`/version` 返回 `commit`、`config_hash`、检索配置与 rerank 计数，可与本材料逐项核对。

---

## 九、v2.0（给已有 Key 新增版本，2026-10-30）

平台规定同一 Key 的接口地址不可变（`endpoint is immutable`），所以 v2.0 **沿用原地址**，只换线上配置与代码版本。

| 表单字段 | 填写 |
|---|---|
| Version name | `v2.0` |
| Add API URL | `https://113.44.133.30/add`（不变） |
| Search API URL | `https://113.44.133.30/search`（不变） |
| 鉴权 | 沿用 v1.0.0（`Authorization: Token <Memory System Key>`） |
| 固定 Commit | `52de40923ff0f4aa0679d5a64f249e10d1595482`（tag `v2.0`；线上 `/version` 的 `commit` = `52de40923ff0`，与 tag 一致） |
| config_hash | `8b8e31d7bacbafe8`（v1.0.0 为 `92d381b0beb88e5a`；线上 `/version` 的 `config_hash` 与 `config_hash_v2` 均为本值——两条路由在线上等价，见下方说明） |

> 说明：tag `v2.0` 之后 `main` 还有 `a398d44`，只改 `tools/e2e_proxy.py`（本地评测代理）与 README，
> 不涉及 `service/` 任何代码或配置，`config_hash` 与服务输出不变；评测版本以 tag `v2.0`（`52de409`）为准。
>
> **/v2/* 与 /add、/search 的关系（2026-10-31 线上实测）**：`MEMORY_UNIT=round`、`CONTENT_HEADER=1` 写在线上
> `service/.env` 里，对 v1 路由同样生效（代码默认值是 `message` / `0`），所以平台登记的 `/add`、`/search`
> **就是 v2 行为**；`/v2/add`、`/v2/search` 是同一进程、同一库内以 `\x1fv2\x1f` 前缀隔离 user 空间的别名路由，
> 供契约/复验工具在 v2 语义下自检。实测：向 `/add` 写一轮 user+assistant，落库正文为
> `[2023-11-14 22:13] user: …\nassistant: …`，与 `/v2/add` 一致。这样做是因为平台规定
> 同一 Key 的接口地址不可变，v2.0 必须沿用原地址才能被评测到新行为。

改动：线上 `.env` 加 `MEMORY_UNIT=round`、`CONTENT_HEADER=1`——user 消息与紧随的 assistant 回复合成一条记忆，
正文前加 `[YYYY-MM-DD HH:MM] user:` / `assistant:` 来源抬头（原文不改写）；检索/精排/预算零改动。
依据：QA「首次接入或接口行为变更后，先使用独立的兼容性 smoke 检查接口规范」「Smoke 可根据错误信息修正接口、鉴权和运行配置」；
Full 尚未提交。根因与实测见 README §十八：Mem-Gallery 全量 1,711 题端到端 EM 0.324→0.392（TR +0.31、VR +0.15、AR +0.09）。
