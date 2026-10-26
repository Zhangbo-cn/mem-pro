# mem-pro

Agent Memory Leaderboard（记忆之巅）**多模态记忆赛道 / 开源方法榜**的参赛工程：一个只做
`Add / Search` 的记忆服务，同步写入、严格 `user_id` 隔离、确定性检索、按 Answer 预算裁剪证据。

**设计基线**：零第三方依赖（纯标准库）· 零 LLM · Embedding 用 `text-embedding-v4` · 精排可选且不受官方限制。

---

## 一、当前状态（2026-10-26）

| 项 | 状态 | 说明 |
|---|---|---|
| 契约自检 | ✅ **27 / 27** | `tools/contract_check.py`，覆盖 11 类官方约束 |
| 自动化测试 | ✅ **68 项** | `make test`：API 端到端、融合/时态回归、缓存、精排降级、多模态去重、caption 缓存 |
| 幂等 / 确定性 | ✅ | 并发重放只写一次；Search 响应字节级可复现（索引与非索引路径逐位一致） |
| 多模态 | ✅ 可用 | 内联 Base64 解析/存储/回传；图片指纹参与去重；可选 `gpt-4o-mini` 生成图片描述并持久缓存（见 §十四） |
| 部署物料 | ✅ | Dockerfile / compose（透明透传全部 `.env`）/ Caddy / systemd / **GitHub Actions CI（3.10 + 3.12 双跑，numpy 与纯标准库两条路径）** |
| 生产形态实测 | ✅ | 容器 + `text-embedding-v4` 真实 API：契约 27/27、replay 幂等/确定性、smoke 18/18 |
| **真实 Embedding API** | ✅ **已配置** | `service/.env` 内 `EMBED_MODE=api` + `text-embedding-v4`，`doctor` 显示连通性 OK；Key 不入库 |
| 公网 HTTPS 部署 | ❌ 未发布 | 需要一台公网主机 + 域名（见 §五） |
| 检索打分校准 | ✅ **已调优** | α=0.95 / β=0.05 + 时态一致性规则；另有 Mem-Gallery 1,711 条 QA 代理榜（见 §十二） |
| 精排 | ✅ **默认关闭，实测 +0.047 MRR** | `gte-rerank-v2`（API）或本地 `bge-reranker-v2-m3`；`RERANK_TOP=10`，启用后单请求多一次往返 |
| 并发/容量 | ✅ **有工具与闸门** | embedding 全局并发闸门 + listen backlog 512；`make load` 扫描并给出申报区间（见 §十二） |

> **关于「API 配置」**：`service/.env` 已配好 DashScope（`EMBED_MODE=api`），该文件被 `.gitignore` 排除。
> `python3 tools/doctor.py` 现在输出 `通过 9 / 警告 0 / 阻塞 0`。

---

## 二、快速开始（离线，1 分钟）

```bash
# 1) 生成配置（随机 Memory System Key，权限 600）
python3 tools/doctor.py --init

# 2) 体检：本地应为「通过 / 警告 / 阻塞=0」
python3 tools/doctor.py

# 3) 起服务（默认 hash 模式，不联网、不花钱）
python3 service/server.py
# 或 make run
```

另开终端：

```bash
curl -s localhost:8080/health
curl -s -X POST localhost:8080/add -H 'Content-Type: application/json' \
  -H "Authorization: Token $(grep MEMORY_SYSTEM_KEY service/.env | cut -d= -f2)" \
  -d '{"request_id":"r1","user_id":"u1","session_id":"s1",
       "messages":[{"role":"user","content":"Alice lives in Berlin.","timestamp":1704067200000}]}'
curl -s -X POST localhost:8080/search -H 'Content-Type: application/json' \
  -H "Authorization: Token $(grep MEMORY_SYSTEM_KEY service/.env | cut -d= -f2)" \
  -d '{"query":"Where does Alice live?","user_id":"u1","top_k":5}'
```

一键验证（自动起临时服务 + 契约 + 复现性 + smoke）：

```bash
make test      # 64 项单元/端到端测试
make check     # contract 27/27 + replay + local smoke
```

---

## 三、接口契约

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/health` | 否 | 任意 2xx 表示正常；含 DB 探活 |
| GET | `/version` | 是 | 版本与 embedding 配置 |
| GET | `/stats` | 是 | 记忆条数、预算、在途上限 |
| POST | `/add` | 是 | `{request_id, messages[], user_id, session_id}` → `{success, request_id, user_id, session_id}` |
| POST | `/search` | 是 | `{query, options?, user_id, top_k}` → `{data:[{id, content, score, created_at}]}` |

- 鉴权支持 `Authorization: Token <k>`、`Authorization: Bearer <k>`、`X-Api-Key: <k>`，常量时间比较。
- **写后可见**：Add 内同步完成分段 → 向量化 → SQLite 落盘后才返回 200；不返回 202。
- **幂等**：`(user_id, request_id)` 内存锁 + SQLite 主键双重保护，最多 32 次重试也只写一次。
- **隔离**：所有检索强制 `WHERE user_id=?`；`session_id` 不参与过滤。
- **顺序**：返回顺序 = 证据优先序；并列按 score → created_at → id 全序。
- **分数语义**：`score` 是归一化相关性分 ∈ [0,1]（`(α·cos + β·bm25_norm)/(α+β)`），可直接做阈值；不是 RRF 名次分。
- **预算**：token ≤ 109,516（117,760 的 93%）、响应体 ≤ 28 MiB、单响应图片 ≤ 6 张；首条超限会截断正文，装不下就丢尾部。
- **错误语义**：契约错误 422；上游 embedding 失败 503（带 `Retry-After`）；过载 429（带 `Retry-After: 1`）；未知路径 404。

多模态 `content` 支持有序数组：

```json
[{"type": "text", "text": "receipt from cafe"},
 {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}]
```

图片必须**内联 Base64**（JPEG/PNG/WebP）；单图解码后 ≤10 MiB，单次 Add ≤30 MiB；远程 URL 直接 422。

---

## 四、配置真实 Embedding API

> 学术榜/开源方法榜要求 `text-embedding-v4`（DashScope / 阿里云百炼）。Key 只放本地 `.env`，
> `.gitignore` 已排除，**不要提交、不要贴到 issue/聊天**。

1. 打开 <https://bailian.console.aliyun.com/>，登录并**开通百炼（Model Studio）**（需实名）。
2. 右上角头像 → **API-KEY** → 创建 → 复制 `sk-...`。
3. 控制台确认已开通按量付费；调试期充 50–100 元足够。
4. 写入配置：

```bash
python3 tools/doctor.py --init        # 若还没生成
# 编辑 service/.env：
#   EMBED_MODE=api
#   DASHSCOPE_API_KEY=sk-你的key
python3 tools/doctor.py               # 应显示 DashScope 连通性 ✅
```

5. 启动并复验：

```bash
python3 service/server.py
python3 tools/contract_check.py http://127.0.0.1:8080 --key "$(grep MEMORY_SYSTEM_KEY service/.env | cut -d= -f2)"
```

成本量级：约 **0.0005 元/千 token**；800M tokens ≈ 400 元。相同文本命中进程内缓存（默认 2 万条）不重复计费。

---

## 五、部署（公网 HTTPS）

官方要求：HTTPS、URL 不含凭据、不能指向私有/回环地址；平台单请求最长 30 分钟。

### 方案 A：Docker Compose + Caddy（推荐）

```bash
cp service/.env.example .env          # 填 EMBED_MODE=api / DASHSCOPE_API_KEY / MEMORY_SYSTEM_KEY
DOMAIN=mem.example.com docker compose --profile tls up -d
```

Caddy 自动申请证书；`deploy/Caddyfile` 已把反代超时设为 31 分钟。
CI 模板在 `deploy/ci.yml`：复制到 `.github/workflows/` 并在 token 具备 `workflow` scope 后推送即可启用。

### 方案 B：systemd + 宿主机 Caddy

```bash
sudo useradd -r -s /usr/sbin/nologin mempro
sudo mkdir -p /opt/mem-pro && sudo cp -r service tools /opt/mem-pro/
sudo cp deploy/mem-pro.service /etc/systemd/system/
sudo systemctl enable --now mem-pro
```

Caddyfile 示例见 `service/README.md` §三。

### 发布后验收

```bash
python3 tools/doctor.py --url https://mem.example.com --key "$KEY"
python3 tools/smoke.py  https://mem.example.com --key "$KEY" --report smoke.json
python3 tools/contract_check.py https://mem.example.com --key "$KEY"   # 27/27
```

提交平台时填：Add `https://mem.example.com/add`、Search `https://mem.example.com/search`、
Authentication `Authorization: Token`、Memory System Key = 你生成的那个。

---

## 六、命令一览

| 命令 | 作用 |
|---|---|
| `make init` / `tools/doctor.py --init` | 生成 `service/.env`（随机 key，600 权限） |
| `make doctor` | 配置体检：模式/Key/连通性/SQLite/端口/鉴权/精排 |
| `make run` | 启动服务（自动加载 `.env`） |
| `make test` | 64 项单元 + 端到端测试 |
| `make check` | 临时服务上跑契约 27 项 + replay + smoke |
| `make contract` / `make replay` | 对已启动服务做契约 / 确定性检查 |
| `make smoke URL=https://... KEY=...` | 线上 smoke（18 项） |
| `make load URL=... KEY=... USER_ID=... SWEEP=1,4,8,16,32` | 并发扫描 Add / Search，输出 p50/p95/p99 与**申报并发区间** |
| `make gallery` / `tools/gallery_proxy.py` | Mem-Gallery 本地代理榜（走线上 Add/Search 代码路径，无需额度） |
| `make image` / `make docker-up` | 构建镜像 / compose 启动 |

---

## 七、目录结构

```
mem-pro/
├── service/                 # 服务本体（零依赖）
│   ├── server.py            # HTTP、鉴权、幂等锁、并发闸门、结构化日志
│   ├── embed.py             # text-embedding-v4 + hash 离线 + LRU 缓存
│   ├── store.py             # SQLite：记忆/向量/幂等；全局递增 seq
│   ├── retrieve.py          # dense + BM25 → 加权融合 → 时态微调 → 去重 → 可选精排
│   ├── budget.py            # token / 字节 / 图片三重裁剪
│   ├── caption.py           # 可选 gpt-4o-mini 图片描述 + SQLite 持久缓存
│   ├── dotenv.py            # 极简 .env 加载器
│   └── .env.example
├── tools/
│   ├── doctor.py            # 配置体检 / --init
│   ├── contract_check.py    # 官方 11 类约束 → 27 项
│   ├── replay.py            # 幂等 + 确定性
│   ├── smoke.py             # 线上 smoke + JSON 报告
│   ├── search_load.py       # Add/Search 并发负载、延迟分位数、申报并发建议
│   ├── gallery_proxy.py     # Mem-Gallery 本地代理榜（Recall/MRR，按 QA 类型拆分）
│   ├── ann_bench.py         # ANN 选型：精确 vs IVF vs PQ vs HNSW（召回/延迟/构建）
│   ├── atm_proxy.py         # ATM-Bench 本地代理榜（检索指标）
│   ├── rerank_server.py     # 本地 TEI 风格精排服务
│   └── rerank_bench.py      # 精排质量与单 query 延迟对比
├── tests/                   # unittest（无 pytest 依赖）
├── scripts/verify.sh        # 一键契约 + replay + smoke
├── deploy/                  # Caddyfile / systemd unit
├── Dockerfile · docker-compose.yml · Makefile
├── data/                    # 小样本数据（大文件已忽略）
└── research/                # 16 份调研 + 原始核验数据
```

---

## 八、MVP 验收清单

- [x] 同步 Add → 200 前完成持久化，写后立即可检索
- [x] `(user_id, request_id)` 幂等（含并发）
- [x] 严格 `user_id` 隔离，`session_id` 不过滤
- [x] `data[]` 顶层数组、`top_k` 上限、空结果 `{"data": []}`
- [x] 多模态 `ContentPart[]` + Base64 校验 + 10/30 MiB 限制
- [x] Answer 117,760 token 预算下的前缀裁剪与图片配额
- [x] 三种鉴权头 + `/health` 免鉴权
- [x] 确定性检索与字节级可复现响应
- [x] 结构化日志、429/503 语义、优雅退出
- [x] 单元 + 端到端测试、契约自检、CI
- [x] Docker / compose / Caddy / systemd 部署物料
- [x] **配置真实 `DASHSCOPE_API_KEY` 并跑通 api 模式**（仅本机 `.env`）
- [ ] **部署公网 HTTPS 并通过 `tools/smoke.py`**
- [x] 本地精排集成与留出集评测（`RERANK_TOP=10`，默认关闭）
- [ ] ATM-Bench 本地代理榜调参（`tools/atm_proxy.py`)

---

## 九、已知边界

1. **Embedding 凭据**：本机 `.env` 已配置 DashScope；仓库不包含 Key。离线 `hash` 模式用于验证契约，不代表线上语义检索效果。
2. **图片不进入向量，只进入检索文本**：图片按 Base64 存储/回传并计入去重指纹；图片内容靠平台 caption 或可选 `gpt-4o-mini` 描述转成文本后参与检索（见 §十四）。查询图片同理——没有图像编码器，纯图片查询只有在带 caption 时才有语义。
3. **精排默认关闭**：默认线性 dense + BM25。两种接法都实测过（§十一 本地模型、§十五 API）：质量提升明显，
   但每次 Search 多一次跨网络往返、吞吐减半，是否开启按时间预算取舍。
4. **单机 SQLite，不引入 ANN**：按 `user_id` 精确全量打分；热用户复用有界记忆、BM25 倒排与向量矩阵/行缓存，返回头部用部分排序，Add 走追加式索引扩展（实测见 §十一）。ANN 选型见 `research/15-ann-index-selection.md`：官方 `user_id` 是会话级，11k 条时精确点积仅 3 ms，而最佳 ANN 只省 2.8 ms 且召回掉 0.4–5%，因此保留精确检索，并按该文的阈值（单用户 ≥5 万条或点积 P95 ≥20 ms）再评估。
5. **未做 LLM 抽取**：正文保留原文与时间戳，满足「时间粒度不归一化」的判分要求；后续若接 `gpt-4o-mini` 只能附加字段。
6. **并发**：见 §十二。`MAX_INFLIGHT` 默认 64，embedding 有独立全局闸门（`EMBED_CONCURRENCY`）；stdlib 线程服务单进程 GIL 受限，Full 前必须用 `make load` 在 api 模式实测，申报值取稳定区间的 60–70%。

---

## 十、调研索引（research/）

| 文件 | 内容 |
|---|---|
| `00-INDEX.md` | 索引、方法、来源、置信度 |
| `01-track-differences.md` | 三赛道 10 维对比 + 推荐 |
| `02-papers.md` | 27 篇论文（arXiv API 核验） |
| `03-projects.md` | 16 个开源项目（GitHub API 实测） |
| `04-models.md` | 模型选型与 FAQ 05 双分支 |
| `05-tech-stack.md` | MVP/冲榜架构、接口契约、成本 |
| `06-open-method-track-checklist.md` | 材料清单、配额、17 天倒排 |
| `07-open-questions.md` | 口径冲突、待澄清 |
| `08-differentiation.md` | 差异化路径 |
| `09-memory-loss-and-scenario-design.md` | 失忆机制与场景设计 |
| `10-positional-effects-and-ordering.md` | 位置效应与输出顺序 |
| `11-ordering-semantics-and-generality.md` | 顺序语义、通用性取舍 |
| `12-multimodal-differentiation.md` | 多模态三组能力与杠杆 |
| `13-multimodal-direction-candidates.md` | 可做方向打分、13 天路线 |
| `14-cycle2-site-recheck.md` | 官网复核：四条硬墙、榜单映射、信息缺口 |

**最终决策（2026-10-14）**：组别 = 开源方法榜；赛道 = 多模态记忆。
`记忆之巅QA全文.md` 为官方材料原文整理，`MISSING.md` 保留第一轮本地检索结论。

---

## 十一、检索打分校准（2026-10-24 实测）

原实现是纯 RRF 名次融合，有两个实测缺陷：**① BM25 全零时按文档插入顺序打分**（最早写入的分块白拿名次分）；
**② 只取名次丢掉幅度**，cos 0.64 与 0.27 被压成 0.0320 / 0.0325，`score` 上限恒 ≈0.0328，无法当阈值。

### 改法

| 项 | 旧 | 新（默认） | 开关 |
|---|---|---|---|
| 融合 | RRF（名次） | `α·cos⁺ + β·bm25_norm`，按有效权重归一 | `FUSION=linear\|rrf` |
| 稀疏零命中 | 参与名次（缺陷） | 整体弃权，只由 dense 定序 | 内建 |
| BM25 校准 | — | `sat`：`s/(s+k)`，k=正分均值，弱匹配保持弱 | `SPARSE_NORM=max\|sat` |
| 稀有词过滤 | — | 只让 df/N ≤ 0.15 的词进 BM25（证件号/型号/专名） | `BM25_RARE_DF` |
| 中文分词 | 仅 bigram | bigram + 单字（「下周几开会」↔「会议」共享「会」） | `CJK_TOKENS` |
| 时态一致性 | — | 问「现在」压纯旧事实、问「以前」抬旧事实；新旧混述记忆保持中立 | `TEMPORAL_RULE` / `TEMPORAL_WEIGHT` |
| `score` | RRF 分（≤0.0328） | 归一化相关性 ∈ [0,1] | — |

### 调参方法（可复现）

```bash
python3 tools/gen_eval.py                  # 生成 60 docs / 100 queries（确定性）
python3 tools/tune.py --set all --step 0.1 # α/β 网格 + 配对 bootstrap
python3 tools/tune.py --set all --split holdout   # 留出集复核
```

标注集：手写 45 docs/49 queries（现实场景）+ 生成 60 docs/100 queries（cross-lingual / paraphrase / needle / temporal），共 **149 条 query**。
embedding 用真实 `text-embedding-v4`，带磁盘缓存，重跑不再计费。

### 结果（α/β 网格，all 集）

| 配置 | MRR@10 | nDCG@5 | R@5 |
|---|---|---|---|
| 旧 RRF（稀疏全零也投票） | 0.929 | 0.938 | 0.980 |
| linear α=1.0 β=0.0（纯 dense） | 0.932 | 0.946 | 0.993 |
| **linear α=0.95 β=0.05（默认）** | **0.932** | **0.953** | **0.993** |
| linear α=0.85 β=0.15 | 0.936 | — | — |
| linear α=0.5 β=0.5 | 0.882 | 0.891 | 0.940 |
| linear α=0 β=1.0（纯 BM25） | 0.722 | 0.745 | 0.853 |

配对 bootstrap（10000 次重采样，vs 纯 dense 基线）：全量集上 α=0.85/β=0.15 的 Δ=+0.0041、P(更好)=0.73；α=0.95/β=0.05 的 Δ≈0。分半复核方向不一致：even 调参半集更偏向纯 dense，odd 留出半集的 α=0.9/β=0.1 MRR=0.963（纯 dense=0.942）。因此保留 α=0.95/β=0.05 作为保守默认，不把单一 split 的最优点直接固化。

### 检索缓存与单次耗时

6,742 条邮件记忆（1,024 维，`tools/atm_proxy.py` 的 ATM-Bench 文本数据切片，hash 向量），单进程实测：

| 场景 | 旧路径 | 当前实现 |
|---|---|---|
| 单次 `run()` P50（numpy） | 469 ms | **8–10 ms** |
| 单次 `run()` P50（纯标准库） | 353 ms | **83 ms** |
| HTTP Search P50（1 并发，numpy） | — | **25 ms** |
| HTTP 吞吐（numpy / 纯标准库） | 3.9 req/s | **35–41 / 10 req/s** |
| Add 后重算索引 | 176–391 ms 全量重建 | **50–65 ms 追加** |
| Add 后首次 Search | 整表重读 | **64 ms** |

同一数据上 top-10 排序与逐文档精确计算逐位一致（BM25/dense 分值差为 0）。用户缓存修正了容量估算——旧估算把 6.7k 条算成 74 MiB、超过上限，导致每次 Search 都重读整表；现在 Add 只追加缓存与索引。生产建议装 numpy（Dockerfile 已装，纯标准库仍可跑，`doctor` 会提示当前路径）。这些是刻意构造的规模压力测试，不代表官方数据分布。

### 精排留出评测

本地 `bge-reranker-v2-m3`，`RERANK_TOP=10`，74 条 odd 留出 query：MRR@10 从 0.949 提升到 0.980，nDCG@5 从 0.961 提升到 0.985，top1 从 0.905 提升到 0.959；CPU 平均约 1.0 秒/query。精排仍默认关闭，避免把模型资源和额外延迟强加给所有部署。

可用 `python3 tools/tune.py --split holdout --rerank-limit 20 --rerank http://127.0.0.1:8010/rerank` 做按 query 类型分层抽样；省略 `--rerank-limit` 则跑完整 split。`tools/rerank_server.py` 默认只监听回环地址。

### 时态一致性规则（2026-10-25 实测）

原实现里 `Alice 现在住在哪个城市？` 会被「Alice 去年住在慕尼黑」抢到第一：纯 cos 不区分时态。
现在加了一条**零依赖、确定性**的规则（`TEMPORAL_RULE=tense`，默认开启）：

- 查询侧：命中「现在/目前/目前/当前/currently/now…」→ `present`，命中「以前/去年/曾经/used to/last year…」→ `past`，两者都有或都没有 → 不启用。
- 记忆侧：正文只含旧时标记 → 视为旧事实；同时含新旧标记（「去年在慕尼黑，现在住柏林」）→ **中立**，因为这种记忆往往正是答案。
- 打分：`present` 时对旧事实减 `TEMPORAL_WEIGHT`，`past` 时加同样权重，最后裁到 `[0,1]`。权重 0.06 只够改写近分平局，压不过强 dense 赢家。

留出集实测（α=0.95/β=0.05，`text-embedding-v4`）：

| 集合 | MRR@10 | nDCG@5 | top1 | temporal 类 MRR |
|---|---|---|---|---|
| holdout，规则关闭 | 0.949 | 0.961 | 0.905 | 0.909 |
| **holdout，规则开启** | **0.976** | **0.981** | **0.959** | **1.000** |
| hand 调参半集，关闭 → 开启 | 0.920 → **0.940** | 0.936 → 0.951 | 0.840 → 0.880 | 0.750 → **1.000** |

没有任何 query 类型退化（逐类检查见 `tools/tune.py --temporal`）。代价：旧事实标记按正文缓存，6.7k 条首次枚举约 66 ms，之后每次查询只多一次标记查表（热路径 P50 8.7 → 9.6 ms）。

```bash
python3 tools/tune.py --set all --split holdout --temporal            # 规则 A/B + 逐类差值
python3 tools/tune.py --set hand --split tune --temporal --temporal-weight 0.06
```

规则只解决「问现在 / 问过去」这一类冲突；更细的时间推理（多久前后、事件排序、跨事件时序）仍需精排或专门模型。关闭方式：`TEMPORAL_RULE=off`。
## 十二、容量与并发申报（2026-10-26 实测）

平台对并发是**申报即承诺**：报高了扛不住会大面积 429（平台遇 429 会等 60 秒），报低了跑不完 Full，
且**只有一次机会**。瓶颈不在 SQLite，而在 DashScope 的 QPM/TPM。

已做的三件事：

1. **embedding 全局并发闸门**（`EMBED_CONCURRENCY`，默认 16）：进程内所有 Add/Search 共用一个信号量，
   超配额只会自己排队，不会把上游打成 429 → 我们的 503 → 平台退避。
   同时一个 Add 的多批 embedding 改为并行（受同一信号量约束），大 Add 不再串行等 N 个来回。
   `/version` 暴露 `api_calls / api_retries / api_429 / api_wait_ms`，可直接看排队与限流。
2. **listen backlog 512**（`LISTEN_BACKLOG`）：Python `HTTPServer` 默认 `request_queue_size=5`，
   32 并发同时建连时 SYN 被丢、客户端 1s/3s 重传，实测 p95 从 70 ms 飙到 1,200 ms 且吞吐掉到 1/10。
   改完后 64 并发 p95 ≈ 150 ms。
3. **`make load` 并发扫描**：给出每档并发的成功率、吞吐、p50/p95/p99，并直接算出「最高稳定并发 → 申报 60–70%」。

### 实测（2026-10-26，`text-embedding-v4`，真实 API，单进程）

先看 hash 模式（16 核 / 15 GB，100 请求/档，完全没有上游延迟）：

| 并发 | Search 吞吐 | Search p95 |
|---|---|---|
| 1 | 255 req/s | 6 ms |
| 8 | 491 req/s | 20 ms |
| 32 | 455 req/s | 71 ms |
| 64 | 359 req/s（99/100） | 199 ms |

**hash 模式的数字只说明服务本身不是瓶颈**。api 模式（每次 Search 要一次真实的 query embedding，
`--nonce` 保证每条 query 都是新文本、不吃进程内缓存）的结论完全不同：

| 并发 | Search 吞吐 | p50 | p95 | 上游 429 |
|---|---|---|---|---|
| 1 | 0.9 req/s | 1.43 s | 2.17 s | 0 |
| 8 | 7.7 req/s | 0.65 s | 1.85 s | 0 |
| 24 | **14.1 req/s** | 1.44 s | 1.75 s | 0 |
| 48 | 14.0 req/s（**饱和**） | 0.92 s | 4.06 s | **23** |

Add（每次 10 条消息 = 1 次 embedding 调用）：

| 并发 | Add 吞吐 | p50 | p95 |
|---|---|---|---|
| 2 | 1.7 req/s | 0.55 s | 2.67 s |
| 6 | 3.8 req/s | 0.65 s | 2.89 s |
| 12 | **4.6 req/s** | 2.53 s | 3.54 s |

三条可以直接用的结论：

1. **瓶颈就是 DashScope**：这个账号的 `text-embedding-v4` 稳定在 **≈14 次 embedding/s**，
   再往上只会换来 429（48 并发时 23 次），吞吐一点不涨、p95 从 1.75 s 涨到 4 s。
2. **单次 Search 的下限是 1.2–1.4 s**（一次 query 向量化的往返），吞吐 ≈ 并发 / 单次延迟。
   想要 14 req/s 只能靠并发 16–24；靠加机器没用，除非换多个账号/多云分流。
3. **429 已经被吸收**：上面 23 次 429 全部由 `embed.embed` 内部重试消化，`valid=60/60`，
   平台侧看不到失败。这正是闸门 + 重试要的效果——宁可在我们这里排队，也不要让平台看到 503。

申报值取两者中**更低**的稳定区间的 60–70%，并同时看 `/version` 的 `api_429` 是否为 0。

---

## 十三、Mem-Gallery 本地代理榜（2026-10-26 新增）

`tools/gallery_proxy.py` 把仓库里的 Mem-Gallery 数据（20 段对话 / 3,962 轮 / 1,711 条 QA）
灌进**线上同一套 Add/Search 代码路径**（进程内调用 `server.do_add` / `do_search`，
不是另写一套打分），从而在无额度、可复现的前提下比较「写入粒度、去重、时态、预算」的影响：

- 每个 QA 的证据轮次（`clue`，如 `D8:2`）落在返回前 k 条里 = 命中；
- 指标：Recall@k、Hit-all@k（全部证据都进前 k）、MRR，按官方 capability（FR/VR/VS/TTL/MR/TR/KR/CD）拆分；
- `AR`（拒答题）没有 clue，只统计平均返回条数；
- 图片本来不在仓库里，用**确定性占位 PNG**（字节由 image_id 决定）走完整的图片解码/指纹/预算路径；
- 文本用数据自带的 caption，等价于「平台随图发送描述」的场景；
- `--add-by session|round` 切换写入粒度（会话级 / 轮次级），默认按会话一次 Add（贴近平台分块）。

```bash
python3 tools/gallery_proxy.py --limit 5                 # 冒烟（hash，秒级）
python3 tools/gallery_proxy.py --k 5,10,20,100           # 全量 hash 约 13 秒
EMBED_MODE=api python3 tools/gallery_proxy.py --report gallery.json   # 真实向量
EMBED_MODE=api python3 tools/gallery_proxy.py --split a   # 分半调参 / 留出复核互换
```

`text-embedding-v4` 全量基线（20 段对话 / 1,711 条 QA，top_k=100，容器同款管线）：

| 类型 | n | MRR | R@5 | R@10 | R@20 | R@100 | All@100 |
|---|---|---|---|---|---|---|---|
| VS（视觉相似） | 306 | 0.827 | 0.855 | 0.934 | 0.978 | 1.000 | 1.000 |
| TTL（时间线） | 337 | 0.904 | 0.766 | 0.933 | 0.979 | 0.993 | 0.985 |
| MR | 206 | 0.729 | 0.717 | 0.809 | 0.898 | 0.987 | 0.966 |
| VR（视觉推理） | 174 | 0.725 | 0.663 | 0.809 | 0.893 | 0.988 | 0.971 |
| FR | 219 | 0.659 | 0.614 | 0.738 | 0.838 | 0.984 | 0.950 |
| TR（时间推理） | 123 | 0.572 | 0.602 | 0.749 | 0.861 | 0.972 | 0.951 |
| CD（计数） | 81 | 0.562 | 0.581 | 0.719 | 0.807 | 0.994 | 0.988 |
| KR（知识更新） | 81 | 0.542 | 0.576 | 0.730 | 0.860 | 0.979 | 0.963 |
| **ALL（不含 AR）** | 1527 | **0.745** | 0.711 | 0.837 | 0.913 | **0.989** | 0.975 |

hash 模式（离线词袋）同一数据只有 MRR 0.385 / R@100 0.852，**绝对值没有意义、只能看方向**；
正式结论一律用 `EMBED_MODE=api` 重跑。为省时省费，工具内置两个开关：

- **并行预热**：Add 是同步的，不预热就是「等 1.3 s → 写 → 等 1.3 s」。先把全部待向量化文本
  并行灌进缓存，全量 9,626 条 **152 s → 19 s**（2 段）／后段 Add 平均 3.9 ms；
- **磁盘向量缓存** `data/.cache/gallery-vecs.db`：同一 `(模型, 维度, 文本)` 跨进程只算一次，
  改权重/规则/排序的 A/B 完全免费（每次约 15 s）。

已用 a/b 交替分半（866 / 866 条量级）做过的实测，结论都写回了默认值：

| 变体 | split a（调参） | split b（留出） | 决定 |
|---|---|---|---|
| α=0.95 β=0.05（默认） | 0.725 | **0.761** | 保留 |
| α=0.90 β=0.10 | 0.732 | 0.761 | 持平，A 集上是短答案偏好（R@20 下降），不换 |
| α=0.70 β=0.30 | 0.737 | — | MRR 升但 R@100 0.983→0.976、All@100 0.964→0.942，**否决** |
| `DEDUP_COS` 关闭 / 0.99 | — | 0.761 / 0.761 | 代理榜同文不同轮次，无近重复可去，**无效** |
| `EMBED_CONTEXT=1`（相邻消息进向量） | — | **0.709** | 明显变差，**保持默认关闭** |
| `TEMPORAL_RULE=off` | 0.585(TR) | 0.558(TR) | 与开启逐位相同：这份数据没有问题以「现在/去年」改写冲突，规则在此**中性**（在自建 149 条集上 +0.027 MRR） |

代理榜已经抓到一个真实缺陷：英文时态线索原来是子串匹配，`know`/`snowing` 命中 `now`、
`performer` 命中 `former`，导致大量英文问题被误判成「问现在」而压掉旧事实。改成
ASCII 词边界匹配后（`(?<![a-z0-9])…(?![a-z0-9])`，中英混排如「Bob now住在哪」仍能命中），
`tools/tune.py --temporal` 的 A/B 差值不变，误触发归零，并补了回归测试。

另外试过「向量化时拼接同一 Add 内相邻消息作上下文」（`EMBED_CONTEXT=1`，只改向量不改正文）：
hash 代理榜上 MRR +0.007 但 R@100 反降 0.031，方向不一致，**默认关闭**，等真实 embedding 复测再定。

---

## 十四、多模态：图片怎么进入检索

平台在 Add 时可能只发图片（`content` 里只有 `image_url`），Search 也可能带图。当前实现：

| 环节 | 做法 |
|---|---|
| 存储 | Base64 原样存（`memories.images`），Search 按 `ContentPart[]` 回传，不改写平台给的 URL/顺序 |
| 去重 | 新增 `img_sig` 列（有序图片指纹的 sha256）：文本相同但图片不同 → 不同记忆；向量相似去重只在 `img_sig` 相同的候选间比较 |
| 检索文本 | 优先用平台随图发来的 caption（作为 text part 进入正文）；`CAPTION_MODE=openai` 时用 **`gpt-4o-mini`** 生成描述并追加 |
| 复现 | 描述按 `(图片 sha256, 模型, prompt 版本, detail)` 落 SQLite 缓存，`temperature=0`、`seed=7` ⇒ 复测零调用、逐字一致；`CAPTION_STRICT=1` 时失败则 Add 返回 503 让平台重试，避免索引不可复现 |

只发图片、没有任何文本的轮次，早期版本统一写成 `(image only) [images:1]`，会被向量相似去重合并成一条
（等于丢掉除第一张外的所有图片）。现在图片指纹进入去重键，回归测试
`test_12_distinct_image_only_memories_all_returned` 覆盖这条路径。

`gpt-4o-mini` 是开源方法榜**唯一允许**的 LLM，且只在 Add/Search 阶段用；默认 `CAPTION_MODE=none`
（只用平台 caption），打开前请同时确认 `OPENAI_API_KEY` 与预算。

---

## 十五、精排：两个真实后端都量过了（2026-10-26）

规则里精排器不限（Add 侧才限 `gpt-4o-mini`），所以这里对比了「本地模型」和「API」两种接法。

| 接法 | 后端 | 留出集 | MRR | 单 query 额外延迟 |
|---|---|---|---|---|
| 无精排 | — | split a / b | 0.725 / 0.761 | 0 |
| 本地 | `bge-reranker-v2-m3`（CPU） | 74 条自建 odd 集 | 0.949 → 0.980 | ≈1.0 s |
| API | DashScope `gte-rerank-v2` | split a / b | **0.781 / 0.808** | ≈0.4 s（同区内网） |

代理榜 split b 的分类别变化（`RERANK_TOP=10`）：TR 0.558→0.652、VR 0.725→0.827、KR 0.542→0.624、
VS 0.827→0.901、MR 0.729→0.827；只有 CD（计数）略降 0.562→0.531。**推理类问题获益最大**，
符合「融合分只能粗筛、交叉编码器才看得懂约束」的直觉。

`RERANK_TOP` 10 与 30 的质量差只有 0.003（0.808 vs 0.811），但候选翻三倍、延迟与费用同涨，
所以默认 10 不变。

**代价（真实 API 实测，24 并发、`--nonce` 强制每条 query 新文本）**：

| | Search 吞吐 | p50 | p95 |
|---|---|---|---|
| 无精排 | 14.1 req/s | 1.44 s | 1.75 s |
| 有精排 | 7.7 req/s | 1.85 s | 3.61 s |

单次 Search 变成两次串行 API 调用（查向量化 → 精排），吞吐正好减半。若 Full 有时间预算压力，
这是唯一需要权衡的开关；按 1,711 条 query 估算，开精排总共约 3.7 分钟，通常值得。

**确定性**：同一 `(query, documents)` 连续 4 次调用 `gte-rerank-v2`，分数逐位一致（实测 `0.94887` ×4）。
但精排是**旁路**：上游抖动时会降级为融合序，排序就变了。压测后实测触发过一次
（契约第 11 项「3 次响应字节一致」失败），所以精排调用加了**瞬时故障重试**
（`RERANK_RETRIES=2`，只重试 429/5xx/超时/断连，4xx 立刻降级），并在 `/version` 暴露
`calls/fails/degraded/cooldown_skips` 四个计数——降级必须能被看见。加完重试后，
连跑 40 请求 × 24 并发再验契约：`fails=0`，27/27 全过。

启用（把三行填进 `.env`，`RERANK_MODE` 不填就是关）：

```bash
RERANK_MODE=http
RERANK_KIND=dashscope
RERANK_URL=https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
RERANK_MODEL=gte-rerank-v2
RERANK_API_KEY=$DASHSCOPE_API_KEY
```

**为什么仍然默认关**：它会把每台部署都变成「必须有外网 + 必须有 rerank 额度」，
而上游抖动时会改变排序。默认关、文档写明收益与代价、`make doctor` 提示，比默认开更稳。
