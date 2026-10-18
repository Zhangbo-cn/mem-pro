# service/ · Add / Search 服务

纯标准库实现，**不需要 pip install**。当前状态：契约自检 **27/27**，单元+端到端测试 **27 项**，
幂等/确定性通过；默认 `hash` 离线模式，正式评测需配置 DashScope Key。

```
service/  server.py    HTTP、鉴权、幂等锁、并发闸门、结构化日志、优雅退出
          embed.py     text-embedding-v4（DashScope）或离线 hash；LRU 缓存
          store.py     SQLite：记忆 + 向量 + 幂等表；全局递增 seq
          retrieve.py  dense + BM25 → RRF → 去重 → 可选精排
          budget.py    117,760 token / 28 MiB / 图片配额 三重裁剪
          dotenv.py    极简 .env 加载器（无第三方依赖）
tools/    contract_check.py  11 类 27 项契约自检
          replay.py          幂等 + 确定性检查
          doctor.py          配置体检 / 生成 .env
          smoke.py           线上 smoke
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
| `MEMORY_SYSTEM_KEY` | 空（不鉴权） | 生产必须设置 |
| `DB_PATH` | service/mem.db | 相对路径按仓库根解析 |
| `MAX_INFLIGHT` | 64 | 超出返回 429 + Retry-After |
| `MAX_IMAGES` | 6 | 单次 Search 图片配额 |
| `STARTUP_CHECK` | 1 | api 模式启动时探活 |
| `DEDUP_COS` | 0.97 | 近重复阈值 |
| `RECALL_N` | 50 | 预留候选池参数 |
| `RERANK_MODE` | none | `http` 时启用精排 |
| `RERANK_URL` / `RERANK_MODEL` / `RERANK_API_KEY` | — | 精排端点 |
| `RERANK_KIND` | tei | `tei` / `dashscope` |
| `RERANK_TOP` / `RERANK_TIMEOUT` | 30 / 8s | 精排条数与超时 |
| `APPEND_OPTIONS` | 0 | 是否把 `options` 拼进 query |
| `LOG_JSON` | 1 | JSON 结构化日志 |

预算常量在 `budget.py`：`TOKEN_BUDGET=109,516`、`BYTE_BUDGET=28 MiB`、`IMG_TOKEN_COST=2,500`。

## 六、已知边界

1. **精排默认关闭**：dense+BM25+RRF；开启 `RERANK_MODE=http` 后失败会自动降级，不阻断 Search。
2. **无 LLM 抽取**：正文保留原文与时间戳，满足「时间粒度不归一化」；后续接 `gpt-4o-mini` 只能附加字段。
3. **多模态只用文本检索**：图片 Base64 存下并按需回传，查询图片不参与向量化。
4. **单机 SQLite**：按 `user_id` 分片暴力检索，10 万条/用户以内足够；更大需 ANN。
5. **并发**：stdlib 线程服务器，Full 前务必压测；申报并发取实测 60–70%。
