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
| 固定 Commit | ☐ 填 `git rev-parse HEAD`（线上 `/version` 的 `commit` 必须与之相同） |
| 配置指纹 | ☐ 填 `/version` 的 `config_hash`（16 位；改任何排序参数都会变） |
| 公开仓库 | https://github.com/Zhangbo-cn/mem-pro |
| 联系人 / 团队 | ☐ |
| 赛道 / 组别 | 多模态记忆赛道 / **开源方法榜**（组别只能选一个） |
| 接口地址 | ☐ `https://<domain>/add`、`https://<domain>/search`（公网 HTTPS） |
| 鉴权方式 | `Authorization: Token <Memory System Key>`（同时接受 `Bearer` / `X-Api-Key`） |
| 允许公开展示 | ☐（建议：方法说明、架构图、评测分类指标；不含任何 Key、不含评测数据样本） |

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
| 时态 | 出现 "last/latest/最新/之前" 等线索时按 `created_at` 降序微调（权重 0.06） | 时序/更新类问题 |
| 去重 | 向量余弦 ≥ 0.97 视为近重复，保留更早者 | 减少多轮重复表达挤占名额 |
| 精排 | 可选交叉编码器 recross（默认 top-10 候选）；**分差在 1e-3 容差内视为并列**，按融合分/时间/ID 定序 | 上游分数存在 1e-4 级抖动，容差+名次映射保证字节可复现 |
| 裁剪 | 单响应 token ≤ 109k、字节 ≤ 28 MiB、图片 ≤ 6 张 | 平台的 Answer 侧预算 |

### 2.4 多模态披露（官方要求明示）

| 问题 | 回答 |
|---|---|
| 是否使用**原图** | **是**。原图原样入库并在命中时作为 `image_url` 内联返回；图片参与去重指纹 |
| 是否使用 **caption** | 平台下发的 caption **原样参与检索文本**（不改写、不二次生成）。系统内置可选 `CAPTION_MODE=openai`，**本期评测保持 `none`**，即不额外调用任何视觉模型 |
| 是否使用 **OCR** | **否** |
| 是否**返回图片证据** | **是**，单响应最多 6 张，超出按融合分优先保留 |

---

## 三、原始工作引用与方法改动说明

### 3.1 引用

| 原始工作 | 用在哪 | 引用 |
|---|---|---|
| BM25 | 稀疏通道打分与饱和归一化 | Robertson & Zaragoza, *The Probabilistic Relevance Framework: BM25 and Beyond*, FnTIR 2009 |
| RRF | 可选融合模式（默认关闭，用线性融合） | Cormack et al., *Reciprocal Rank Fusion*, SIGIR 2009 |
| 交叉编码器精排 | 可选精排通道（默认开启与否见 §五 运行说明） | 模型服务：阿里云百炼 `qwen3-rerank`（`gte-rerank-v2` 官方标注 2026-05-30 下线） |
| 文本向量 | 稠密通道 | 阿里云百炼 `text-embedding-v4`（Qwen3-Embedding 系列） |
| 近重复抑制 | 检索结果去重 | 余弦阈值法，本仓库自实现 |

### 3.2 改动说明

本仓库**全部代码自研**（`service/` 约 3k 行、`tools/` 约 2.3k 行、测试 76 项），未复用任何论文官方实现或第三方框架代码，因此无「相对原始工作的代码级改动」需披露。参考的仅为上述通用算法思想，实现细节（融合权重、时态线索匹配、去重阈值、证据预算、确定性处理）均为本团队在公开数据集 Mem-Gallery 与 ATM-Bench 上实测调出的。

---

## 四、容量与运行限制声明

| 项 | 值 |
|---|---|
| 部署形态 | 单机 Docker（Python 3.12-slim + numpy），1 容器 + 可选 Caddy 反代 |
| 资源 | 2 vCPU / 2 GiB / 40 GiB 起（实测 29 req/s 时 CPU 0.52 核、RSS 242 MiB） |
| **申报并发（上界）** | Search **16** / Add **16**（依据：`--duration` 持续压测 100% 成功档；burst 全过不作为依据） |
| 平均延迟 | Search p50 ≈ 0.5 s、p95 ≈ 1.7 s（含向量化往返；开启精排 p50 ≈ 1.2 s） |
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
curl -s https://<domain>/version  # commit 与 config_hash 必须与提交材料一致
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
| 2 | 联系人 / 机构 | ☐ 待填 |
| 3 | 赛道 + 组别 | ✅ 多模态 / 开源方法榜 |
| 4 | 方法说明 + 引用 | ✅ §二、§三 |
| 5 | 在线 Add / Search（HTTPS 公网） | ☐ 待部署（`deploy/LOCAL_DEPLOY.md`） |
| 6 | 鉴权方式声明 | ✅ §一 |
| 7 | 容量与运行限制 | ✅ §四 |
| 8 | 公开 GitHub 仓库 | ✅ |
| 9 | 固定 Commit | ☐ 冻结后填 |
| 10 | 原始工作引用 | ✅ §3.1 |
| 11 | 方法改动说明 | ✅ §3.2 |
| 12 | 可公开展示的信息范围 | ☐ 待填 |
| 13 | 依赖与运行说明 | ✅ §五 |
| 14 | 原创性披露 + 诚信承诺 | ✅ §六 |
| 15 | Memory System Key | ☐ 走私有渠道提交，**不入仓库** |
