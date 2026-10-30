# mem-pro

Agent Memory Leaderboard（记忆之巅）**多模态记忆赛道 / 开源方法榜**的参赛工程：一个只做
`Add / Search` 的记忆服务，同步写入、严格 `user_id` 隔离、确定性检索、按 Answer 预算裁剪证据。

**设计基线**：零第三方依赖（纯标准库）· 零 LLM · Embedding 用 `text-embedding-v4` · 精排可选且不受官方限制。

---

## 一、当前状态（2026-10-26）

| 项 | 状态 | 说明 |
|---|---|---|
| 契约自检 | ✅ **27 / 27** | `tools/contract_check.py`，覆盖 11 类官方约束 |
| 自动化测试 | ✅ **98 项** | `make test`：API 端到端、融合/时态回归、缓存、精排降级、多模态去重、caption 缓存 |
| 幂等 / 确定性 | ✅ | 并发重放只写一次；Search 响应字节级可复现（索引与非索引路径逐位一致） |
| 多模态 | ✅ 可用 | 内联 Base64 解析/存储/回传；图片指纹参与去重；可选 `gpt-4o-mini` 生成图片描述并持久缓存（见 §十四） |
| 部署物料 | ✅ | Dockerfile / compose（透明透传全部 `.env`）/ Caddy / systemd / **GitHub Actions CI（3.10 + 3.12 双跑，numpy 与纯标准库两条路径）** |
| 生产形态实测 | ✅ | 容器 + `text-embedding-v4` + `qwen3-rerank` 真实 API：契约 27/27（连跑 2 次）、replay 幂等/确定性、smoke 18/18 |
| **真实 Embedding API** | ✅ **已配置** | `service/.env` 内 `EMBED_MODE=api` + `text-embedding-v4`，`doctor` 显示连通性 OK；Key 不入库 |
| 公网 HTTPS 部署 | ❌ 未发布 | 需要一台公网主机 + 域名（见 §五） |
| 检索打分校准 | ✅ **已调优** | α=0.95 / β=0.05 + 时态一致性规则；另有 Mem-Gallery 1,711 条 QA 代理榜（见 §十二） |
| 返回条数 | ⚠️ **默认按 top_k（`MAX_RESULTS=0`）** | 答案 F1 在 10–20 条饱和，但若按 Search 输出评分则收窄伤召回；**待官方答复评分口径**（§十六） |
| 精排 | ✅ **已启用（2026-10-28 定稿）** | `qwen3-rerank`（DashScope API），`RERANK_TOP=30` + `RERANK_BLEND=0.7`（RRF 混合）；a/b 两半 R@10 +0.034/+0.040、MRR +0.007/+0.014 |
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
make test      # 98 项单元/端到端测试
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

**TLS 这一段本地已经端到端跑过**（不用等公网主机）：用同一个 `docker-compose.yml`
（`--profile tls`）+ 一份只把 Caddyfile 换成 `tls internal` 的 override，起在
`https://localhost:8444`，拿容器自己的根 CA 做校验后：

- 契约 **27/27**（含 gzip 压缩、`ContentPart[]` 多模态、响应体积上限、重放一致性）
- smoke **18/18**
- 中间隔着 Caddy 时 Search p50 49 ms（纯内网 + 已预热向量）

也就是说上线时唯一没验证过的只剩「DNS 解析 + Let's Encrypt 签发」这两件外部依赖。

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

### 5.1 部署与运维文档索引

| 文档 | 内容 |
|---|---|
| `deploy/LOCAL_DEPLOY.md` | 本机 / 内网部署、带宽↔吞吐表、方案 A/B/C |
| `deploy/REMOTE_OPS.md` | **远端上线后的运维**：IP 证书 HTTPS、带宽改按流量计费、精排降级核验、SSH 加固、提报字段 |
| `deploy/Caddyfile.ip` | 给公网 IP 直签 Let's Encrypt `shortlived` 证书的 Caddy 配置（绕开备案） |

## 六、命令一览

| 命令 | 作用 |
|---|---|
| `make init` / `tools/doctor.py --init` | 生成 `service/.env`（随机 key，600 权限） |
| `make doctor` | 配置体检：模式/Key/连通性/SQLite/端口/鉴权/精排 |
| `make run` | 启动服务（自动加载 `.env`） |
| `make test` | 98 项单元 + 端到端测试 |
| `make check` | 临时服务上跑契约 27 项 + replay + smoke |
| `make contract` / `make replay` | 对已启动服务做契约 / 确定性检查 |
| `make smoke URL=https://... KEY=...` | 线上 smoke（18 项） |
| `make load URL=... KEY=... USER_ID=... SWEEP=1,4,8,16,32` | 并发扫描 Add / Search，输出 p50/p95/p99 与**申报并发区间**；加 `DURATION=180` 转持续压测（burst 通过 ≠ 稳态可过） |
| `make gallery` / `tools/gallery_proxy.py` | Mem-Gallery 本地代理榜（走线上 Add/Search 代码路径，无需额度） |
| `make e2e SAMPLE=400` | 端到端答案评测：证据喂回答模型，扫 `top_k`，比 EM/F1（见 §十六） |
| `make wan URL=... KEY=...` | 公网上行带宽/吞吐实测（真图 Add + Search，直接算评测总耗时；`tools/wan_bench.py`） |
| `make rehearse URL=... KEY=... FILES=4 N=60` | **远端评测预演**：真图按「单次 Add ≤ 30 MiB」切块写入 + 按申报并发打 Search，量 4xx/5xx、延迟、响应体积、全量外推（`tools/rehearse.py`） |
| `make watch URL=... KEY=... FEISHU=<webhook>` | 评测期看护：定时拉 `/version`，`api_give_up` 增长 / 精排降级 / 429 占比过高时告警 |
| `make backup DB_PATH=... BACKUP=...` | SQLite 在线热备（`sqlite3` backup API，不用停服务） |
| `make purge PREFIX=aml:` | 合规清理：按 `user_id` 前缀删评测数据并 VACUUM（默认 dry-run） |
| `make image` / `make docker-up` | 构建镜像（注入 `GIT_COMMIT`）/ compose 启动 |

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
│   ├── e2e_proxy.py         # 端到端答案评测（证据 → 回答模型 → EM/F1，扫 top_k；--oracle 上限诊断）
│   ├── doc_check.py         # 提交文档 ↔ 代码默认值一致性检查（make check 会跑）
│   ├── wan_bench.py         # 公网上行带宽/吞吐实测（真图 Add+Search，算评测总耗时）
│   ├── rehearse.py          # 远端评测预演（真图切块 Add + 并发 Search，找线上卡点）
│   ├── fetch_gallery_images.sh  # 下载 Mem-Gallery 官方真图（520 MiB，gallery_proxy --real-images 用）
│   ├── rerank_server.py     # 本地 TEI 风格精排服务
│   ├── rerank_bench.py      # 精排质量与单 query 延迟对比
│   ├── watch.py             # 评测期看护：/version 计数器异常告警（可推飞书）
│   ├── purge.py             # 合规清理：按 user 前缀删评测数据 + VACUUM
│   └── backup.py            # SQLite 在线热备（评测中途坏了能救回来）
├── tests/                   # unittest（无 pytest 依赖）
├── scripts/verify.sh        # 一键契约 + replay + smoke
├── deploy/                  # Caddyfile / systemd unit / LOCAL_DEPLOY.md（本地公网部署三方案）
├── TODO.md · RUNBOOK.md · SUBMISSION.md   # 待办 / 评测当天看护流程 / 15 项提交材料
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
- [x] ATM-Bench 本地代理榜交叉验证（`tools/atm_proxy.py`）：邮件模态 381 条，精排 +0.077 MRR
- [x] `/version` 可核验（`commit` + `config_hash`），镜像构建注入 `GIT_COMMIT`
- [x] 合规：日志只留元数据（有测试守护）、日志轮转、`tools/purge.py` 按用户删除
- [x] 精排默认模型换成 `qwen3-rerank`（gte 已标注下线，实测抖动 1.3e-3 / 4 次顺序翻转）
- [x] 评测期看护 `tools/watch.py`（`api_give_up` 增长、精排降级、429 占比告警）
- [x] 提交材料草稿 `SUBMISSION.md`、运行手册 `RUNBOOK.md`、本地部署 `deploy/LOCAL_DEPLOY.md`

---

## 九、已知边界

1. **Embedding 凭据**：本机 `.env` 已配置 DashScope；仓库不包含 Key。离线 `hash` 模式用于验证契约，不代表线上语义检索效果。
2. **图片不进入向量，只进入检索文本**：图片按 Base64 存储/回传并计入去重指纹；图片内容靠平台 caption 或可选 `gpt-4o-mini` 描述转成文本后参与检索（见 §十四）。查询图片同理——没有图像编码器，纯图片查询只有在带 caption 时才有语义。
3. **精排默认关闭**：默认线性 dense + BM25。两种接法都实测过（§十一 本地模型、§十五 API）：质量提升明显，
   但每次 Search 多一次跨网络往返、吞吐减半，是否开启按时间预算取舍。
4. **单机 SQLite，不引入 ANN**：按 `user_id` 精确全量打分；热用户复用有界记忆、BM25 倒排与向量矩阵/行缓存，返回头部用部分排序，Add 走追加式索引扩展（实测见 §十一）。ANN 选型见 `research/15-ann-index-selection.md`：官方 `user_id` 是会话级，11k 条时精确点积仅 3 ms，而最佳 ANN 只省 2.8 ms 且召回掉 0.4–5%，因此保留精确检索，并按该文的阈值（单用户 ≥5 万条或点积 P95 ≥20 ms）再评估。
5. **未做 LLM 抽取**：正文保留原文与时间戳，满足「时间粒度不归一化」的判分要求；后续若接 `gpt-4o-mini` 只能附加字段。
6. **并发**：见 §十二。`MAX_INFLIGHT` 默认 64，embedding 有独立全局闸门（`EMBED_CONCURRENCY`）；stdlib 线程服务单进程 GIL 受限，Full 前必须用 `make load ... DURATION=180` 在 api 模式做**持续**压测，申报值取「100% 成功」的那一档（实测 16）。

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
3. **`make load` 并发扫描**：给出每档并发的成功率、吞吐、p50/p95/p99，并直接算出「最高稳定并发 → 申报 60–70%」。**加 `DURATION=180` 才是持续压测**，申报值只看后者。

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

| 并发 | Search 吞吐 | p50 | p95 | 说明 |
|---|---|---|---|---|
| 1 | 0.9 req/s | 1.43 s | 2.17 s | 改造前单次调用 1.4 s，瓶颈是**每次新建 TCP+TLS** |
| 24（改造前） | 14.1 req/s | 1.44 s | 1.75 s | 撞上游 QPS 墙，429 靠内部重试吃掉 |
| **24（现在）** | **16.3 req/s** | **0.50 s** | 1.9 s | 三次中位：16.3 / 18.2 / 14.9 req/s |
| 48（现在） | 21–25 req/s | 0.59 s | 2.5 s | 再往上就是账号配额墙 |

Add（每次 10 条消息 = 1 次 embedding 调用，同样 `--nonce`）：4 并发 5.6 req/s、
12 并发 9.4、24 并发 12.6、48 并发 **23.4 req/s**（p50 1.05 s、p95 1.9 s）。
连接池之前这一档 12 并发只有 4.6 req/s，差别全在握手。

四条可以直接用的结论：

1. **最大的性能问题不是算法，是连接方式**。`urllib.request.urlopen` 每次调用都新建
   TCP+TLS，实测单条 embedding 在 0.3–1.9 s 之间抖（握手占 0.5–1.0 s），并发一高就是
   握手风暴。换成本项目的 `service/http_pool.py`（`http.client` keep-alive 池，标准库）后：
   单条调用 **1.3 s → 0.2–0.35 s**，24 并发 p50 **1.44 s → 0.50 s**，48 并发从
   60 s 超时（45/60 通过）变成 p95 2.5 s（60/60）。
2. **瓶颈仍然是 DashScope 配额**：这台机器 16–25 req/s 就封顶，再压只换来 429 与 p95 上涨。
   看到 `api_429` 涨就把 `EMBED_CONCURRENCY` 往回退。
3. **单次 Search 的下限 ≈ 一次 embedding 往返（+ 一次精排往返）**，即 0.2–0.4 s；
   要更高吞吐只能靠并发 16–24，或换多账号 / 多云分流。
4. **429 必须由我们吸收，而且重试要有时间墙**：上面那些 429 全在 `embed.embed` 内部消化，
   平台侧看到的是 200。但 5 次指数退避能撑到 60 s，把请求和闸门槽一起占死、p95 爆表；
   `EMBED_RETRY_BUDGET=12` 到点就失败成 503 + `Retry-After`，把槽让给健康请求更划算。

**但短 burst 全过不等于可持续**。真实评测是持续流量（1,711 条 query 连续打），
所以用 `--duration` 做稳态复核（每档 3 分钟、每条 query 新文本）：

| 并发 | 稳态吞吐 | 状态码分布 | p95 |
|---|---|---|---|
| 8 | 14.9 req/s | 200 ×2,695（**100%**） | 1.36 s |
| **16** | **27.9 req/s** | 200 ×5,120 / 503 ×1（99.98%） | 1.65 s |
| 24 | 29.0 req/s | 200 ×5,289 / 503 ×24（99.55%） | 2.71 s |

Add 侧稳态（2 分钟）：12 并发 19.8 req/s 100% 200、16 并发 **27.6 req/s 100% 200**。

结论：**申报 Search 16 / Add 16**。16 与 24 的吞吐已经一样（29 req/s 是账号天花板），
但 24 并发开始零星 503（0.45%）、p95 从 1.65 s 涨到 2.71 s——多报的并发不会更快，
只会把余量换成重试。按 16 算，1,711 条 query 的 Search 阶段约 **61 秒**跑完。
跑完看 `/version` 的 `api_429`，涨得厉害就再退一档；Full 只有一次机会，低报比高报安全。

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

### 14.1 真图实测（2026-10-26）

之前所有测试都用 1×1 占位图。现在拉了 Mem-Gallery 官方图片（HuggingFace `Ethan-Bei/Mem-Gallery`，
1,490 张 / 520 MiB）：`bash tools/fetch_gallery_images.sh` → `gallery_proxy.py --real-images`。

| | `MAX_IMAGES=6`（默认） | `MAX_IMAGES=3` |
|---|---|---|
| Add / Search 失败 | **0 / 0**（1,711 次 Search） | 0 / 0 |
| Search 响应 p50 / p95 / max | 0.59 / 10.20 / **22.41 MiB** | 0.26 / 6.50 / 14.39 MiB |
| 平均响应 | 2.38 MiB | 1.20 MiB |
| 单图大小 p50 / p90 / max | 41 KB / 776 KB / 18.9 MiB | 同 |

- **全部在 30 MiB 上限内**：`budget.clip` 的字节预算（28 MiB）起作用，max 22.4 MiB。
- **内存差点 OOM**：真图库（505 MiB DB）下并发 Search，RSS 峰值 4 并发 0.7 / 16 并发 1.2 / **32 并发 1.76 GiB**，
  超过 compose 的 1.5 GiB 限额。根因是 glibc 每线程一个 malloc arena，几 MiB 的 Base64 在各 arena 里碎片化不归还。
  镜像 / systemd / `make run` 统一设 `MALLOC_ARENA_MAX=2` 后峰值 **0.52 / 0.57 / 0.60 GiB**，吞吐不变（20–24 req/s）。
  另把 Search 取图改为按 10 条一批、够 `MAX_IMAGES×2` 张就停（输出字节不变，已比对 1,711×2 次响应 hash）。
- 官方集里有 **7 张超过 10 MiB 单图上限**——平台按规则不会发，代理榜照此跳过；万一平台真发了，服务端 422（不静默截断）。
- **带宽重新估算**：旧表按每图 150 KB 估，实际平均每次响应 2.38 MiB，20 Mbit 跑完 1,711 条要 **≈29 分钟**
  而不是 6 分钟。新表见 `deploy/LOCAL_DEPLOY.md`。带宽 < 50 Mbit 时建议 `MAX_IMAGES=3`
  （体积减半；对答案的影响量不了——官方 Answer 模型是否看图、看几张都不知道）。

### 14.2 公网 2 Mbit 下端到端预演（2026-10-28）

`tools/rehearse.py` 打真机（`https://113.44.133.30`），按申报并发发 Search，只走网络、不走进程内。

**修之前**（`MAX_IMAGES=3`、原图直传）：一条响应 p50 4.81 MiB、p95 12.12 MiB、max 20.49 MiB。

| 项 | jobs=1 | jobs=2 |
|---|---|---|
| Search 延迟 p50 / p95 | 18.6s / 48.5s | **31.3s / 88.8s** |
| 吞吐 | 0.040 req/s | 0.039 req/s |
| 全量 1,711 条外推 | 12.0 小时 | 12.3 小时 |

- **并发在 2 Mbit + 大响应下是负作用**：单流已能跑满 ~2.1 Mbit（`curl` 实测 250–278 KB/s），
  两条流同时抢把 p95 从 48s 抬到 89s，吞吐一点没多，还出现过「客户端零字节、Caddy→服务端 Send-Q 堆积 3.4 MB」的 stall。
- **瓶颈是响应体积，不是服务端**：同时间服务端 `/search` 日志 180–450ms、精排 0 degraded/0 fail、
  `load` 近 0、内存 0.9/1.7 GiB。3 张原图 Base64 后一张就能有 2.58 MiB，比 100 条文本（~40 KB）大两个数量级。

**修之后**（`IMG_MAX_SIDE=1024` + `IMG_QUALITY=82`，`service/imgsize.py`）：

| jobs | Search 响应 p50 / p95 / max | 延迟 p50 / p95 / max | 吞吐 | 全量外推 |
|---|---|---|---|---|
| 1 | 0.33 / 0.63 / 0.67 MiB | 1.48s / 3.56s / 5.01s | 0.177 req/s | 161 分钟 |
| 2 | 同 | 2.37s / 5.55s / 6.79s | 0.204 req/s | 140 分钟 |
| 3 | 同 | 3.52s / 7.88s / 19.2s | **0.243 req/s** | 117 分钟 |
| 6 | 同 | 6.50s / 16.4s / 25.5s | 0.237 req/s | 120 分钟 |

- 同一批图 **2.71 MiB → 0.29 MiB（9.3×）**，一条响应 2.74 MiB → 0.32 MiB，3 次请求字节完全一致（可复现）。
- 服务端 `apt install python3-pil`（Pillow 10.2.0）；缺 Pillow 或 `IMG_MAX_SIDE=0` 时行为与历史版本逐字节一致。
- 只改 Search 响应字节，**库里的原图不动**（仍按单图 ≤10 MiB 原样入库）。
- 并发甜点在 2–3：6 并发吞吐不再涨、p95 反而 16s。**申报 Search 3 / Add 4**（上行不计费也不限速，Add 瓶颈在 embedding）。
- 全量 1,711 条出网约 600 MiB ≈ ¥0.5，**约 2 小时**跑完（原 12 小时）。

## 十五、精排：两个真实后端都量过了（2026-10-26）

规则里精排器不限（Add 侧才限 `gpt-4o-mini`），所以这里对比了「本地模型」和「API」两种接法。

| 接法 | 后端 | 留出集 | MRR | 单 query 额外延迟 |
|---|---|---|---|---|
| 无精排 | — | split a / b | 0.725 / 0.761 | 0 |
| 本地 | `bge-reranker-v2-m3`（CPU） | 74 条自建 odd 集 | 0.949 → 0.980 | ≈1.0 s |
| API | `gte-rerank-v2`（已标注下线） | split a / b | 0.781 / 0.808 | ≈0.24 s（同区内网） |
| API | **`qwen3-rerank`（现默认）** | split a / b | **0.791 / 0.798** | ≈0.32 s（同区内网） |

两个 API 模型质量打平（两半均值都是 0.795），但**确定性差 4 个数量级**（见下），
而且 `gte-rerank-v2` 官方标注 2026-05-30 下线 —— 所以默认模型改成 `qwen3-rerank`。

代理榜 split b 的分类别变化（当时 `RERANK_TOP=10`）：TR 0.558→0.652、VR 0.725→0.827、KR 0.542→0.624、
VS 0.827→0.901、MR 0.729→0.827；只有 CD（计数）略降 0.562→0.531。**推理类问题获益最大**，
符合「融合分只能粗筛、交叉编码器才看得懂约束」的直觉。

**换一份数据交叉验证（防止只在 Mem-Gallery 上调参）**：`tools/atm_proxy.py` 跑 ATM-Bench 的
邮件模态切片（6,742 条邮件记忆 × 1,013 条查询，剔除证据不在语料里的 632 条，剩 **381 条**）：

| | Recall@1 | Recall@5 | Recall@10 | MRR@10 | nDCG@10 |
|---|---|---|---|---|---|
| 无精排 | 0.671 | 0.894 | 0.935 | 0.784 | 0.818 |
| `gte-rerank-v2`（rtop 30） | 0.773 | 0.935 | 0.950 | 0.861 | 0.880 |
| **`qwen3-rerank`（rtop 30）** | **0.777** | **0.944** | **0.958** | **0.863** | **0.885** |

一份是「多会话待办 + 图片」，一份是「个人邮件 + 多跳数字」，精排的收益都是 +0.05 ~ +0.08 MRR，
方向一致 —— 说明这不是在 Mem-Gallery 上过拟合出来的。代价也一致：P50 192 ms → 1,134 ms（`gte`）
/ 1,998 ms（`qwen3`，长邮件文本下更慢；本系统里精排只吃单条消息，实际只 +0.08 s）。

**候选深度与混合权重（2026-10-28，a/b 两半各跑一遍）**：一开始只比了 10 与 30 的 MRR
（差 0.003），看不到真实结构。这次把 `RERANK_TOP` 扫到 5 档、并按类别拆开，问题才露出来：

| `RERANK_TOP`（纯精排） | MRR a/b | R@10 a/b | TTL R@10 | FR R@10 |
|---|---|---|---|---|
| 10 | 0.791 / 0.798 | 0.814 / 0.855 | **0.929** | 0.728 |
| 20 | 0.789 / 0.801 | 0.832 / 0.876 | 0.864 | 0.782 |
| 30 | 0.788 / 0.802 | 0.842 / 0.883 | 0.841 | 0.811 |
| 50 | 0.786 / 0.801 | 0.839 / 0.881 | 0.818 | 0.823 |
| 100 | 0.774 / 0.792 | 0.826 / 0.870 | 0.792 | 0.824 |

- **召回不是问题**（`R@100` 五档全是 0.983/0.994，一条没变），变的是头部排序。
- 纯精排从 10 加到 30：FR/MR/CD/VR 的前 10 召回各涨 7~8 个点，但 **TTL（时序）掉 8.8 个点，
  MRR 掉 0.044** —— 交叉编码器看不懂「去年/现在」这类约束，把融合序里本来对的时序证据挤出去。
- 所以加 `RERANK_BLEND`：精排名次与融合名次各算 `1/(k+rank)`（k=10）按 w 加权，`w=1` 即纯精排
  （与历史行为逐字节一致）。只改顺序，`score` 仍按名次映射回融合区间。

| 变体 | MRR a/b | R@10 a/b | TTL R@10 | 八类里退步的 |
|---|---|---|---|---|
| 纯精排 top10（旧默认） | 0.791 / 0.798 | 0.814 / 0.855 | 0.929 | — |
| **top30 + w0.7（新默认）** | **0.805 / 0.805** | **0.854 / 0.889** | 0.881 | 无（TTL R@10 −0.049，但 MRR +0.010） |
| top30 + w0.5 | 0.791 / 0.799 | 0.847 / 0.890 | 0.907 | FR MRR 0.741（低于基线） |
| top50 + w0.7 | 0.806 / 0.806 | 0.848 / 0.890 | 0.885 | 与 top30 打平，候选多 67% |

**取 `RERANK_TOP=30` + `RERANK_BLEND=0.7`**：两半 MRR 都涨、R@10 +0.034/+0.040，除 TTL 的 R@10
（−0.049，纯精排是 −0.088）外八类全涨；50 只多 R@20 0.003，不值候选翻 67% 的延迟。

**代价（真实 API，24 并发、`--nonce`、各跑 3 次取中位）**：

| | Search 吞吐 | p50 | p95 |
|---|---|---|---|
| 无精排（burst，24 并发） | 16.3 req/s | 0.50 s | 1.9 s |
| 有精排（burst，24 并发，`gte`） | 13.8 req/s | 1.18 s | 2.2 s |
| 无精排（**持续 120 s，16 并发**） | 27.9 req/s | 0.21 s | 1.65 s |
| 有精排（**持续 120 s，16 并发，`qwen3`**） | **28.0 req/s** | 0.33 s | 1.24 s |

持续压测下开精排不掉吞吐（瓶颈在上游配额而不是本地 CPU），只是 p50 多 0.12 s；
3,389 条请求里 `rerank.fails=0`、`degraded=0`，契约第 11 项（3 次字节一致）仍通过。

单次 Search 变成两次串行调用（query 向量化 → 精排），吞吐只降约 15%，但 p50 多 0.7 s。
按 1,711 条 query 估算，开精排总共约 3.7 分钟，通常值得。

**确定性踩了两个坑，都修了**：

1. 精排是**旁路**：上游抖动会降级为融合序，排序就变了。压测后实测触发过一次
   （契约第 11 项「3 次响应字节一致」失败）。→ 加**瞬时重试**（`RERANK_RETRIES=2`，只重试
   429/5xx/超时/断连，4xx 立刻降级），并在 `/version` 暴露
   `calls/fails/degraded/cooldown_skips`——降级必须能被看见。
2. 更隐蔽：`gte-rerank-v2` 的分数**不是逐位可复现**的，而且是**双峰**的——同一
   `(query, documents)` 连续 12 次调用，分数在 `0.104684` 和 `0.104543` 两套精确值之间跳
   （差 1.4e-4），像是有两台后端各算一遍。扩到 24 条真实 query × 3 次，最大偏差 **1.3e-3**、
   48 次里有 **4 次顺序直接翻转**；同样条件下 `qwen3-rerank` 偏差 **6.4e-8**、**0 次翻转**。
   这个分数会原样写进响应 `score` 字段，于是「同一 query 重放字节一致」被破坏（排序没变、字节变了）。
   **量化治不了**：任何网格都会在边界上翻转（先试过 1e-3 取整，仍能复现失败）。两手改：
   - 顺序：分差 `< RERANK_TOL`（0.0003，≈2× 观测到的双峰间距）一律按**并列**处理，
     交给确定性的「融合分降序 → `created_at` 降序 → id 升序」决定。于是无论上游落在
     哪个变体，只要两个变体的间距小于容差，合并后的等价类就完全一样；
   - 分数：响应里的 `score` 改为按**名次**等距映射回融合分区间，与上游分值大小无关，
     字节只依赖最终顺序。

   改完：代理榜 MRR 0.808 → 0.809（抖动级别），同一 query 连续 10 次响应字节一致，
   24 并发压测后契约仍 27/27。

启用（把三行填进 `.env`，`RERANK_MODE` 不填就是关）：

```bash
RERANK_MODE=http
RERANK_KIND=dashscope
RERANK_URL=https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
RERANK_MODEL=qwen3-rerank
RERANK_API_KEY=$DASHSCOPE_API_KEY
```

**为什么仍然默认关**：它会把每台部署都变成「必须有外网 + 必须有 rerank 额度」，
而上游抖动时会改变排序。默认关、文档写明收益与代价、`make doctor` 提示，比默认开更稳。

---

## 十六、端到端答案评测：召回高 ≠ 答得对（2026-10-26）

上面所有数字都是**召回**指标，但平台最终按**答案**打分（`记忆之巅QA`：平台统一 Answer + 评分）。
所以补了一个端到端代理 `tools/e2e_proxy.py`：用我们真实返回的证据喂一个便宜的回答模型
（`qwen-flash`，temperature 0），跟官方答案比对，量 EM / Contains / token-F1。

> 平台用的是它自己的回答模型，我们只是**代理**：绝对值不可直接对比官方 F1，但**相对比较**有效
> （同一提示词、同一模型、同一批 QA，只换被评测的那一个变量）。
>
> **局限必须说清**：仓库里的 Mem-Gallery 只有 `image_caption`，`data/mem-gallery/image/` 是空的
> ——没有真实图片字节。所以这里量的是 **caption 通道**：能回答「caption 够不够用来答题」，
> **不能**回答「给原图比给 caption 多赚多少」。后者只能在官方 Full 或自备图集上量。

全量 1,711 条 QA × 5 档 top_k（8,555 次调用，18.0M token，约 23 分钟）：

| top_k | EM | Contains | token-F1 |
|---|---|---|---|
| 5 | 0.333 | 0.455 | 0.504 |
| **10** | **0.342** | **0.477** | **0.527** |
| 20 | 0.341 | 0.465 | 0.521 |
| 50 | 0.331 | 0.448 | 0.514 |
| 100 | 0.332 | 0.434 | 0.505 |

**答案在 10–20 条就饱和了，100 条反而略降。** 不是召回的问题（R@100 比 R@10 高 0.18），
而是提示被塞长之后模型注意力被稀释——最典型的是 TTL（"图里这个产品叫什么"）：
top10 EM 0.33 → top100 **0.17**，一多半的分数被 90 条无关条目挤掉了。KR（更新类）反过来，
顶到 100 才 0.41（0.32 @10）——它需要跨会话比对，条目多才有料。

于是加了一个开关 `MAX_RESULTS`：契约只要求「返回数量不得超过 top_k」，收窄是合法的，
`MAX_RESULTS=20` 能让响应体积降约 1/3、答案质量不变。

**但默认仍是 0（老实按 top_k）**，原因是评分口径没定：

| 口径 | 依据 | `MAX_RESULTS=20` 的效果 |
|---|---|---|
| 按 Answer 输出打 F1 | 官方原文「统一回答模型」「Mem-Gallery 使用 F1」 | F1 +0.016，TTL EM +0.11 |
| 按 Search 输出（含顺序）打分 | research/08–10 记录的 2026-10-14 口头澄清 | R@100 **0.70 → 0.35**、All@100 0.53 → 0.22（hash 同数据） |

两边最优值相反，而 Full 只能跑一次，所以在拿到官方书面答复前取两边都不吃大亏的 0。
拿到答复后只改 `.env` 一行（`config_hash` 会随之变化，需同步到提交材料）。

**精排在答案端量不出收益**（同 400 条分层子集，其余配置完全一致）：

| | EM@10 | EM@20 | EM@50 | F1@10 | F1@20 | F1@50 |
|---|---|---|---|---|---|---|
| 无精排 | 0.323 | **0.361** | 0.354 | 0.491 | **0.527** | 0.514 |
| 开精排 | 0.321 | 0.341 | **0.366** | 0.484 | 0.506 | **0.532** |

两行互有胜负、差值都在 ±0.02（n=400 的 1σ 约 0.023），**结论是「没有可测的答案收益」**。
这和 §十五 的 MRR +0.04 并不矛盾：精排改善的是「第 3 条变第 1 条」，而答案模型只要证据
在 top-10 里就够用。既然答案端量不出收益，它就不值得成为 Full 的硬依赖
（多一个外部服务 + 一次上游抖动风险）——**Full 建议关精排**，这与「宁可 MRR 低 0.05 也要可复现」
的原则一致。

```bash
# 全量扫描（约 ¥3、23 分钟）
EMBED_MODE=api DASHSCOPE_API_KEY=$KEY python3 tools/e2e_proxy.py --top-ks 5,10,20,50,100
# 只比两种配置（400 条分层子集，3 分钟）
EMBED_MODE=api DASHSCOPE_API_KEY=$KEY python3 tools/e2e_proxy.py --sample 400 --top-ks 10,20,50
```

### 16.1 上限诊断（oracle）：检索还值不值得继续调

`--oracle` 直接把**金标 clue 那一轮**当证据喂给同一个回答模型，等于「检索完美」。
**全量 1,711 条**（初版只跑了 297 条、每类 33 条，单类误差 ±0.15，有一条结论被全量推翻，见下）：

| | EM | token-F1 |
|---|---|---|
| 真实检索 top-10 | 0.342 | 0.527 |
| **oracle（完美检索）** | **0.535** | **0.705** |

| 类型 | n | 检索 EM | oracle EM | 差 | 差的 95% CI |
|---|---|---|---|---|---|
| VR（数图） | 174 | 0.287 | 0.730 | **+0.443** | ±0.094 |
| VS（选图） | 306 | 0.552 | 0.908 | **+0.356** | ±0.064 |
| TTL（认图） | 337 | 0.332 | 0.614 | **+0.282** | ±0.072 |
| AR（该弃答） | 184 | 0.533 | 0.696 | +0.163 | ±0.098 |
| CD（矛盾） | 81 | 0.296 | 0.358 | +0.062 | ±0.144（不显著） |
| KR（更新） | 81 | 0.321 | 0.370 | +0.049 | ±0.146（不显著） |
| MR（回忆） | 206 | 0.078 | 0.097 | +0.019 | ±0.055（不显著） |
| TR（先后） | 123 | 0.260 | 0.276 | +0.016 | ±0.111（不显著） |
| FR（列举） | 219 | 0.269 | 0.283 | +0.014 | ±0.084（不显著） |

结论（只写显著的）：

1. **差距几乎全在三类视觉题（VR/VS/TTL，共 817 条，占 48%）**，都显著。它们的共同点是
   **答案在一两条特定记忆里，而 top-10 里有 8–9 条同话题干扰项**：数「那天有几张车辆照片」、
   选「哪张是 AI 应用」时，干扰图片 caption 让模型数错/选错。这是**证据集形状**问题，不是召回问题
   （这三类 R@10 已经 0.8+）。
2. **AR 显著**（+0.16）：干扰证据会诱发硬答。
3. **TR/KR/CD/MR/FR 不显著**：即使检索完美，回答模型也答不好——瓶颈在回答侧推理，不在我们。
   *初版 297 条时报告「FR 反而 −0.12」，全量是 +0.014 —— 那条是小样本噪声，已撤回。*

## 十七、三条"看起来该有用"的规则，实测全部落在噪声里

按 §16.1 的诊断方向试了三个方向，全部 a/b 两半验证（`gallery_proxy --split a|b`，
EMBED_MODE=api，top_k=100）。**结论：全部默认关闭**，代码留着并由 `DATE_WEIGHT` /
`RECENCY_RULE` / `DIVERSITY` 开关控制，附实测数字以免后人重复踩。

| 规则 | 想法 | split a | split b | 结论 |
|---|---|---|---|---|
| 日期匹配 `DATE_WEIGHT` | 问题里写了 `2024-06-17` → 那天写入的抬上来 | MRR 0.725→0.729（TR +0.016） | 0.761→0.761 | 只命中 **14/1,711** 条问题，信号量不够 |
| 更新类新鲜度 `RECENCY_RULE` | "latest / 最新" → 按写入时间抬新压旧 | 0.725→0.722（**KR −0.033**） | 0.761→0.761 | 反向：KR 问"最新立场"时最新那条常不是最完整的陈述 |
| 多样性 `DIVERSITY`（MMR-lite） | 多线索题要覆盖不同事实，同话题条目不要占满 | 0.725→0.728（All@10 KR −0.058） | 0.761→0.763（VR **−0.033**） | 互有胜负；VR 需要同一场景的多张近图，压相似度正好反着来 |

另外试了「绝对分数阈值 ⇒ 低分直接返回空」来救 AR：top-1 分数的分布 AR 与非 AR 几乎重叠
（阈值 0.5 时能弃掉 23% 的 AR，代价是误弃 8% 的正常问题），净收益 **−0.013 EM**，弃。

**这说明什么**：公开子集的检索召回已经到顶（R@100 0.98–0.99，All@100 0.96–0.98），
剩下 12 个点的差距在**证据集形状**（要不要按日期/会话收窄）和**回答侧推理**，
不在"再调一个打分项"。继续调参数的边际收益接近 0，这也是为什么后面把精力转去部署与提交材料。

---

## 十八、v2：smoke 0 分项的根因与修复（2026-10-30）

**现象**（官方 multimodal smoke，v1.0.0）：总分 15；Retrieval 66.67（atomic_retrieval 100 / **direct_recall 0**），
Relational reasoning **0**（multi_session_reasoning 0），Evidence governance **0**（abstention 0）。

**根因**：平台 Answer 模型**只读 `content`**，不读 `created_at`/`session_id`。v1 一条消息一条记忆、正文只有原话：

1. **没有日期** → 跨会话 / 先后 / 「什么时候」类题，证据里根本没有时间，只能瞎答（本地 TR EM 0.24）；
2. **没有说话人、问答被拆开** → assistant 回复（常含名字/结论）脱离提问不可读，user 一句又缺补充，
   多会话推理拼不起来；
3. **拒答**：100 条孤立碎句里总有「沾边」的，诱发硬答；整轮证据能让模型看清「确实没提过」。

smoke 每项题量很小（总 15 分说明只有个位数题），单题对错就是 0/100 的跳变，不能拿 smoke 分数直接做 A/B；
所以用本地 Mem-Gallery 全量 1,711 题 × 同款 Answer 链路（`tools/e2e_proxy.py`，qwen-flash，top_k=100）量。

**修复（只改 Add 侧的记忆单元与正文格式，检索打分零改动）**：

- `MEMORY_UNIT=round`：user 消息与紧随其后的 assistant 回复合成一条记忆（= 基准 clue `D1:3` 的粒度）；
- `CONTENT_HEADER=1`：正文前加 `[YYYY-MM-DD HH:MM] user:` / `assistant:`——只是来源元数据（时间+角色），原文一字不改；
- 以 **`/v2/add`、`/v2/search`** 暴露（存储空间与 v1 隔离），**`/add`、`/search` 与 v1.0.0 逐字节一致**
  （同一组请求、新旧代码对比输出与 `config_hash=71780ada…`（hash 模式）完全相同），已绑定的 v1 不受影响。

全量 1,711 题，EM（top_k=100）：

| 类型 | n | v1 | v2 | Δ |
|---|---|---|---|---|
| TR（时间/先后，跨会话） | 123 | 0.244 | **0.553** | **+0.309** |
| VR（视觉推理/关系） | 174 | 0.264 | **0.414** | **+0.149** |
| AR（拒答） | 184 | 0.582 | **0.674** | **+0.092** |
| KR（知识更新） | 81 | 0.395 | 0.457 | +0.062 |
| VS | 306 | 0.582 | 0.631 | +0.049 |
| TTL | 337 | 0.178 | 0.208 | +0.030 |
| FR | 219 | 0.265 | 0.288 | +0.023 |
| MR | 206 | 0.083 | 0.097 | +0.015 |
| CD | 81 | 0.333 | 0.284 | −0.049（n=81，1σ≈0.05，噪声内） |
| **ALL** | 1711 | **0.324** | **0.392** | **+0.068**（token-F1 0.506→0.577） |

消融（450 题分层子集）：只开抬头 EM 0.353→0.404，只开轮次 0.353→0.369，两者都开 0.413——**日期/说话人抬头是主因**，
轮次合并主要贡献检索（R@10 0.838→0.856、All@100 0.973→0.982，MRR 持平 0.745→0.742）。

---

## 许可

代码与文档：MIT（见 `LICENSE`）。`data/` 下的第三方基准数据保留原许可，ATM-Bench 为 **CC BY-NC 4.0（仅限非商业）**，见 `data/README.md`。
