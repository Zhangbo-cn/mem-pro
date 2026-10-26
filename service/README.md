# service/ · Add / Search 服务

纯标准库实现，**不需要 pip install**。当前状态：契约自检 **27/27**，单元+端到端测试 **73 项**，
幂等/确定性通过；默认 `hash` 离线模式，正式评测需配置 DashScope Key。

```
service/  server.py    HTTP、鉴权、幂等锁、并发闸门、结构化日志、优雅退出
          embed.py     text-embedding-v4（DashScope）或离线 hash；LRU 缓存
          store.py     SQLite：记忆 + 向量 + 幂等表；全局递增 seq
          retrieve.py  dense + BM25 → 加权融合 → 时态微调 → 去重 → 可选精排
          budget.py    117,760 token / 28 MiB / 图片配额 三重裁剪
          dotenv.py    极简 .env 加载器（无第三方依赖）
tools/    contract_check.py  11 类 27 项契约自检
          replay.py          幂等 + 确定性检查
          doctor.py          配置体检 / 生成 .env
          smoke.py           线上 smoke
          search_load.py     Search 并发负载与延迟分位数
          rerank_bench.py    精排质量与单 query 延迟对比
```

## 一、本地启动

```bash
# 生成 service/.env（随机 Memory System Key）
python3 tools/doctor.py --init

# 离线模式（默认，不联网、不花钱）
python3 service/server.py
# 等价：EMBED_MODE=hash PORT=8080 python3 service/server.py
```

自检（另开终端，key 从 service/.env 读取）：

```bash
KEY=$(grep MEMORY_SYSTEM_KEY service/.env | cut -d= -f2)
python3 tools/contract_check.py http://127.0.0.1:8080 --key "$KEY"   # 期望 27 / 27
python3 tools/replay.py        http://127.0.0.1:8080 5 --key "$KEY"  # 幂等 ✅ / 确定性 ✅
```

## 二、配置真实 API

1. <https://bailian.console.aliyun.com/> 登录 → 开通百炼 → 创建 API-KEY（`sk-...`）。
2. 修改 `service/.env`：

```bash
EMBED_MODE=api
DASHSCOPE_API_KEY=sk-你的key
```

3. 验证：

```bash
python3 tools/doctor.py      # DashScope 连通性 ✅
python3 service/server.py    # 启动时也会探活；失败直接退出
```

Key 只存在本地 `.env`（已 gitignore），不会进入仓库或镜像。

## 三、公网部署

官方要求 HTTPS、不得含凭据、不得指向私有/回环地址；单请求最长 30 分钟。

```bash
# systemd 托管
MEMORY_SYSTEM_KEY=$(openssl rand -hex 24) EMBED_MODE=api DASHSCOPE_API_KEY=sk-xxx \
  PORT=8080 nohup python3 service/server.py >/var/log/aml.log 2>&1 &

# Caddy（自动证书；反代超时 > 30 分钟）
cat >/etc/caddy/Caddyfile <<'EOF'
mem.example.com {
    reverse_proxy 127.0.0.1:8080 {
        transport http {
            read_timeout 31m
            write_timeout 31m
        }
    }
}
EOF
systemctl reload caddy
```

或直接用仓库根目录的 `docker compose --profile tls up -d`（见 `deploy/Caddyfile`、`deploy/mem-pro.service`）。

发布后：

```bash
python3 tools/doctor.py --url https://mem.example.com --key "$KEY"
python3 tools/smoke.py  https://mem.example.com --key "$KEY"
```

## 四、鉴权与接口

设置 `MEMORY_SYSTEM_KEY` 后接受：

- `Authorization: Token <k>`
- `Authorization: Bearer <k>`
- `X-Api-Key: <k>`

`GET /health` 永远免鉴权；`/stats`、`/version` 需要鉴权。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 200 = 正常（含 SQLite 探活，失败 503） |
| GET | `/version` | 版本 + embedding 配置 |
| GET | `/stats` | 记忆数、预算、在途上限 |
| POST | `/add` | 同步写入，200 时已可检索 |
| POST | `/search` | 返回 `{"data":[...]}`，顺序即证据序 |

## 五、参数（环境变量 / .env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` / `HOST` | 8080 / 0.0.0.0 | 监听 |
| `EMBED_MODE` | hash | `hash` 离线 / `api` 真实 |
| `DASHSCOPE_API_KEY` | — | api 模式必填 |
| `EMBED_MODEL` / `EMBED_DIM` | text-embedding-v4 / 1024 | 学术榜强制该模型 |
| `EMBED_BATCH` | 10 | 官方单次上限 10 行 |
| `EMBED_CHUNK_CHARS` | 3000 | 单段字符上限（中文≈3k token） |
| `EMBED_CACHE_MAX` | 20000 | 进程内向量缓存条数 |
| `HTTP_POOL_SIZE` | 32 | 上游 keep-alive 连接池上限（embedding + 精排共用额度上限） |
| `EMBED_TIMEOUT` / `EMBED_RETRY_BUDGET` | 60s / 12s | 单次超时与重试总预算（超预算即 503 + Retry-After） |
| `EMBED_CONCURRENCY` | 16 | 同时发往 DashScope 的请求上限（按 QPM 配额设；超了只会换来 429→503） |
| `EMBED_CONTEXT` | 0 | 向量化时拼接同一 Add 内前后 N 条消息（只影响向量，不改正文） |
| `LISTEN_BACKLOG` | 512 | TCP listen backlog；默认 5 时高并发丢 SYN，p95 飙到秒级 |
| `CAPTION_MODE` | none | `none` 只用平台 caption / `openai` 用 gpt-4o-mini 生成描述 |
| `MEMORY_SYSTEM_KEY` | 空（不鉴权） | 生产必须设置 |
| `DB_PATH` | service/mem.db | 相对路径按仓库根解析 |
| `MAX_INFLIGHT` | 64 | 超出返回 429 + Retry-After |
| `MAX_IMAGES` | 6 | 单次 Search 图片配额 |
| `STARTUP_CHECK` | 1 | api 模式启动时探活 |
| `FUSION` | linear | `linear`（α·cos+β·bm25）/ `rrf`（旧名次融合，A/B 用） |
| `DENSE_WEIGHT` / `SPARSE_WEIGHT` | 0.95 / 0.05 | 融合权重 α / β，实测见根 README §十一 |
| `SPARSE_NORM` | sat | `sat` 饱和校准 / `max` 除以最大值 |
| `BM25_RARE_DF` | 0.15 | 只让 df/N ≤ 该值的稀有词进 BM25；0=关闭 |
| `CJK_TOKENS` | bigram_unigram | `bigram` / `bigram_unigram` / `unigram` |
| `TEMPORAL_RULE` | tense | `tense` 时态一致性微调 / `off` 关闭 |
| `TEMPORAL_WEIGHT` | 0.06 | 时态加分上限（融合分 ∈[0,1]，只改写近分平局） |
| `DEDUP_COS` | 0.97 | 近重复阈值 |
| `USER_CACHE_MAX_BYTES` | 64 MiB | 用户记忆缓存估算上限；0 关闭 |
| `RANK_INDEX_MAX_MB` | 64 | 检索索引（倒排 + 向量矩阵/行）缓存上限；超限自动退回逐文档计算 |
| `RERANK_MODE` | none | `http` 时启用精排 |
| `RERANK_URL` / `RERANK_MODEL` / `RERANK_API_KEY` | — | 精排端点 |
| `RERANK_KIND` | tei | `tei` / `dashscope` |
| `RERANK_TOP` / `RERANK_TIMEOUT` | 10 / 5s | 精排条数与单次超时 |
| `RERANK_RETRIES` / `RERANK_FAILS` / `RERANK_COOLDOWN` | 2 / 3 / 60 | 瞬时重试、熔断阈值与冷却秒数 |
| `RERANK_POOL` | 16 | 精排侧连接池上限 |
| `RERANK_TOL` | 0.0003 | 精排分容差：上游分数双峰抖动 1.4e-4，容差内视为并列并按融合分排序 |
| `APPEND_OPTIONS` | 0 | 是否把 `options` 拼进 query |
| `LOG_JSON` | 1 | JSON 结构化日志 |

本地 `tools/rerank_server.py` 默认只监听 `127.0.0.1`；跨主机访问需显式设置 `RERANK_HOST`，并自行限制网络访问。

Search 的 JSON 日志包含 `search_store_ms`、`search_embed_ms`、`search_rank_ms`、`search_images_ms`、`search_format_ms`，以及记忆数、用户缓存命中（`search_store_cache_hit`）、索引命中（`search_index_cache_hit`）与追加建索引（`search_index_extended`）标志；顶层 `ms` 是整次 HTTP 请求耗时。用已有用户的真实 query 做并发压测：

```sh
python3 tools/search_load.py http://127.0.0.1:8080 --user-id USER_ID --queries-file tools/eval_queries.json --requests 200 --concurrency 16 --key "$MEMORY_SYSTEM_KEY"
```

压测会重复循环 query 文件中的问题，输出成功吞吐及 P50/P95/P99；请在隔离的测试服务上逐级增加并发，避免对正式数据集制造负载。

### 检索缓存与实测性能

每个用户的检索索引（BM25 倒排表 + 向量矩阵/行）按 `(DB_PATH, user_id)` 有界缓存，Add 之后不会整表重建：只要旧索引对应的记忆仍是新列表的前缀，就只追加新文档的倒排与向量行（`search_index_extended=true`）；只有改写或删除时才重建。用户记忆缓存同样按追加维护，容量超限才丢弃。

搜索本身只做“够用”的工作：头部用部分排序（score-only 取头 + 边界平局再用完整 tie-break），去重只贪心到 `top_k`（需要精排时再加 `RERANK_TOP`），向量比较直接复用索引里的矩阵（numpy 下为一次 `pool × pool` 矩阵乘）。

6,742 条邮件记忆（1,024 维，`tools/atm_proxy.py` 数据切片的 ATM-Bench 文本；hash 向量只用于性能对照）：

| 场景 | 旧路径 | 当前实现 |
|---|---|---|
| 单次 `run()` P50，numpy | 469 ms | **8–10 ms** |
| 单次 `run()` P50，纯标准库 | 353 ms | **83 ms** |
| HTTP Search P50（1 并发，numpy） | — | **25 ms** |
| HTTP 吞吐 / 进程（numpy，8–32 并发） | — | **35–41 req/s** |
| HTTP 吞吐 / 进程（纯标准库） | 3.9 req/s | **10 req/s** |
| Add 后重算索引 | 176–391 ms 全量重建 | **50–65 ms 追加** |
| Add 后首次 Search（HTTP P50） | 旧缓存失效后整表重读 | **73 ms** |

同一数据上 top-10 排序与逐文档精确计算逐位一致（BM25/dense 分值差为 0，20 条 query 全部相同）。纯标准库仍可用，但 dense 慢约 10 倍，**生产建议安装 numpy**：Dockerfile 已装，`tools/doctor.py` 会提示当前走哪条路径。

吞吐受单进程 GIL 限制（numpy 下约 25 ms CPU/请求）；要更高并发请按进程数横向扩展，而不是只调大 `MAX_INFLIGHT`。这些是刻意构造的规模压力测试，不代表官方数据分布。

预算常量在 `budget.py`：`TOKEN_BUDGET=109,516`、`BYTE_BUDGET=28 MiB`、`IMG_TOKEN_COST=2,500`。

## 六、已知边界

1. **精排默认关闭**：dense + BM25 线性融合；开启 `RERANK_MODE=http` 后失败会自动降级，不阻断 Search。
2. **无 LLM 抽取**：正文保留原文与时间戳，满足「时间粒度不归一化」；后续接 `gpt-4o-mini` 只能附加字段。
3. **图片不参与向量化**：Base64 存下并按需回传，图片指纹参与去重；语义检索靠平台 caption 或 `CAPTION_MODE=openai` 生成的描述（gpt-4o-mini，按图片 sha256 缓存）。
4. **单机 SQLite，不引入 ANN**：按 `user_id` 做精确全量打分；热用户复用有界记忆、BM25 倒排与向量矩阵/行缓存，返回头部使用部分排序，Add 走追加式索引扩展。选型依据与阈值见 `research/15-ann-index-selection.md`（真实 v4 向量实测：11k 条精确 3.2 ms，ANN 最多省 2.8 ms 但召回 0.95–1.0）。单用户 ≥5 万条或点积 P95 ≥20 ms 时再评估，用 `tools/ann_bench.py` 复测。
5. **并发**：stdlib 线程服务器、单进程 GIL 受限；embedding 调用有全局信号量（`EMBED_CONCURRENCY`），`/version` 暴露 `api_429`/`api_wait_ms`。Full 前用 `tools/search_load.py --sweep` 在 api 模式实测，申报并发取稳定区间的 60–70%，必要时多进程横向扩展。
