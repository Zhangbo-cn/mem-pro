# mem-pro

Agent Memory Leaderboard（记忆之巅）**多模态记忆赛道 / 开源方法榜**的参赛工程：一个只做
`Add / Search` 的记忆服务，同步写入、严格 `user_id` 隔离、确定性检索、按 Answer 预算裁剪证据。

**设计基线**：零第三方依赖（纯标准库）· 零 LLM · Embedding 用 `text-embedding-v4` · 精排可选且不受官方限制。

---

## 一、当前状态（2026-10-18）

| 项 | 状态 | 说明 |
|---|---|---|
| 契约自检 | ✅ **27 / 27** | `tools/contract_check.py`，覆盖 11 类官方约束 |
| 自动化测试 | ✅ **27 项** | 单元 + 真起 HTTP 的端到端测试（含并发幂等） |
| 幂等 / 确定性 | ✅ | 并发重放只写一次；Search 响应字节级可复现 |
| 多模态 | ✅ 基础可用 | 内联 Base64 解析、存储、按 `ContentPart[]` 返回；文本检索 |
| 部署物料 | ✅ | Dockerfile / compose / Caddy / systemd / CI |
| **真实 Embedding API** | ❌ **未配置** | 当前默认 `EMBED_MODE=hash`（离线）；仓库内没有、也不应有 Key |
| 公网 HTTPS 部署 | ❌ 未发布 | 需要一台公网主机 + 域名（见 §五） |
| 精排 | ⚪ 可选未接 | `RERANK_MODE=none`；本地已有 reranker 工具与权重 |
| 多模态理解（caption/OCR） | ⚪ 未接 | 依赖平台传入的 caption；未调用任何 VLM |

> **关于「API 配置」**：代码已完整支持 DashScope，但**没有可用的 `DASHSCOPE_API_KEY`**。
> 运行 `python3 tools/doctor.py` 会明确显示「Embedding API: 未配置」。
> 配 Key 的步骤见 §四，预计 10 分钟（百炼需实名/开通，可能产生少量费用）。

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
make test      # 27 项单元/端到端测试
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
| `make test` | 27 项单元 + 端到端测试 |
| `make check` | 临时服务上跑契约 27 项 + replay + smoke |
| `make contract` / `make replay` | 对已启动服务做契约 / 确定性检查 |
| `make smoke URL=https://... KEY=...` | 线上 smoke（18 项） |
| `make image` / `make docker-up` | 构建镜像 / compose 启动 |

---

## 七、目录结构

```
mem-pro/
├── service/                 # 服务本体（零依赖）
│   ├── server.py            # HTTP、鉴权、幂等锁、并发闸门、结构化日志
│   ├── embed.py             # text-embedding-v4 + hash 离线 + LRU 缓存
│   ├── store.py             # SQLite：记忆/向量/幂等；全局递增 seq
│   ├── retrieve.py          # dense + BM25 → RRF → 去重 → 可选精排
│   ├── budget.py            # token / 字节 / 图片三重裁剪
│   ├── dotenv.py            # 极简 .env 加载器
│   └── .env.example
├── tools/
│   ├── doctor.py            # 配置体检 / --init
│   ├── contract_check.py    # 官方 11 类约束 → 27 项
│   ├── replay.py            # 幂等 + 确定性
│   ├── smoke.py             # 线上 smoke + JSON 报告
│   ├── atm_proxy.py         # ATM-Bench 本地代理榜（检索指标）
│   ├── rerank_server.py     # 本地 TEI 风格精排服务
│   └── rerank_bench.py      # 精排压测
├── tests/                   # unittest（无 pytest 依赖）
├── scripts/verify.sh        # 一键契约 + replay + smoke
├── deploy/                  # Caddyfile / systemd unit
├── Dockerfile · docker-compose.yml · Makefile
├── data/                    # 小样本数据（大文件已忽略）
└── research/                # 15 份调研 + 原始核验数据
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
- [ ] **配置真实 `DASHSCOPE_API_KEY` 并跑通 api 模式**（需要账号 Key）
- [ ] **部署公网 HTTPS 并通过 `tools/smoke.py`**
- [ ] 接入精排（`RERANK_MODE=http`）并重标 `RERANK_TOP`
- [ ] ATM-Bench 本地代理榜调参（`tools/atm_proxy.py`）

---

## 九、已知边界

1. **没有真实 API Key**：仓库永远不包含 Key；`hash` 模式只验证契约与确定性，不代表线上语义检索效果。
2. **多模态只用文本检索**：图片按 Base64 存储/回传，不参与向量化；查询里的图片也不编码（没有图像编码器）。平台会提供 caption。
3. **精排默认关闭**：dense+BM25+RRF 已是可用基线；开启本地 reranker 需先起 `tools/rerank_server.py`。
4. **单机 SQLite**：按 `user_id` 分片暴力检索；单用户 10 万条以内足够，再大需换 ANN。
5. **未做 LLM 抽取**：正文保留原文与时间戳，满足「时间粒度不归一化」的判分要求；后续若接 `gpt-4o-mini` 只能附加字段。
6. **并发**：`MAX_INFLIGHT` 默认 64；Full 前需用 `tools/rerank_bench.py` / 压测确认真实承载，再按实测 60–70% 申报。

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
