# RUNBOOK —— Full 评测当天照着做

只看两件事：**开跑前 15 分钟检查**、**跑起来之后盯什么**。
Full 本届每个 Key 每赛道最多 2 次，且第 2 次要在第 1 次完成后 30 天才能发起
⇒ 实际只有一次机会，别在开跑后改任何东西。

---

## 一、T-15min 开跑前检查

```bash
BASE=https://你的域名          # 不要用 localhost 自测通过就申报
KEY=<Memory System Key>

# 1) 版本冻结：记下这两个值，写进提交材料；跑完对账
curl -s -H "Authorization: Token $KEY" $BASE/version | python3 -m json.tool \
  | grep -E '"version"|"commit"|"config_hash"'

# 2) 库是干净的（上一次 Full 的数据不能在）
curl -s -H "Authorization: Token $KEY" $BASE/stats | python3 -c 'import json,sys;print("memories =",json.load(sys.stdin)["memories"], "(应为 0)")'

# 3) 上游健康：embedding + 精排各打一次
python3 tools/doctor.py

# 4) 外网三件套（必须从外部网络跑，不要本机 localhost）
python3 tools/contract_check.py $BASE --key $KEY     # 27/27
python3 tools/smoke.py $BASE --key $KEY              # 18/18
MEMORY_SYSTEM_KEY=$KEY python3 tools/replay.py $BASE 5   # 幂等 + 5 次字节一致

# 5) 持续压测（唯一可信的容量证据）
python3 tools/search_load.py $BASE --key $KEY --user-id soak:pre \
        --concurrency 8 --duration 180 --nonce
curl -s -H "Authorization: Token $KEY" $BASE/version | python3 -c 'import json,sys;d=json.load(sys.stdin)["embed"];print({k:d[k] for k in ("api_calls","api_429","api_give_up")})'
```

通过标准：契约 27/27、smoke 18/18、replay 一致、压测 100% 成功、`api_give_up == 0`。

- [ ] `sqlite3 /data/mem.db ".backup '/data/snapshot-empty.db'"` 留一个空库快照
- [ ] 磁盘剩余 > 2 GB（`df -h /data`）
- [ ] 宿主机不休眠、不自动更新（见 `deploy/LOCAL_DEPLOY.md` §五）
- [ ] 飞书告警已开（`tools/watch.py` 每 5 分钟拉一次 `/version`）

---

## 二、跑起来之后盯什么

每 5 分钟一条命令：

```bash
watch -n 300 'curl -s -H "Authorization: Token $KEY" $BASE/version' | python3 -c '
import json,sys; d=json.load(sys.stdin); e=d["embed"]; r=d["retrieval"]["rerank"]
print("calls", e["api_calls"], "429", e["api_429"], "retries", e["api_retries"],
      "give_up", e["api_give_up"], "| rerank_fail", r.get("fails"), "degraded", r.get("degraded"))'
```

| 指标 | 正常 | 含义 / 动作 |
|---|---|---|
| `api_429` | 缓慢增长 | 配额墙。只要 `api_give_up` 不涨，就是内部重试吸收了，**不用动** |
| `api_give_up` | 0 | > 0 说明重试预算（`EMBED_RETRY_BUDGET=12`）耗尽、返回了 503。持续增长 → 降并发（要平台重跑，但断点续跑不算新 Full） |
| `api_retries / api_calls` | < 30% | 更高说明上游不稳，观察 |
| `rerank.degraded` | false | true = 精排连续失败已降级，**排序会变**，记下来在材料里说明 |
| 磁盘 | 剩余 > 1 GB | 不足会写库失败（503 db_unavailable） |
| 容器状态 | `healthy` | `docker compose ps`；重启次数 > 2 要查日志 |

日志排查（只有元数据，没有正文）：

```bash
docker compose logs --tail 200 mem-pro | grep -E '"status":5' | tail
docker compose logs --tail 50 mem-pro | grep -c '"status":200'
```

---

## 三、异常处置

| 现象 | 处置 |
|---|---|
| 平台页面报某阶段失败 | **不要删库、不要改版本、不要改 `.env`**。平台支持断点续跑且保持同一 `request_id`；先确认服务 `/health` 正常，再等平台重试 |
| 服务挂了 | `docker compose up -d`（`restart: unless-stopped` 一般会自愈）。起来后先 `/stats` 看记忆条数符合进度，再等平台续跑 |
| 库损坏 | 停服务 → `cp /data/mem.db /data/mem.db.bad` → 恢复最近快照 → 起服务。注意：已写入的 Add 会由平台带同一 `request_id` 重发，响应从 `idem` 表或重新计算，幂等有保证 |
| 上游 401/403 | Key 失效。换 `DASHSCOPE_API_KEY`（同一模型），重启；不改其他配置 |
| IP 变了 / 域名不通 | 见 `deploy/LOCAL_DEPLOY.md` §六 |

---

## 四、跑完

```bash
# 1) 归档这次运行的证据（写进提交材料）
curl -s -H "Authorization: Token $KEY" $BASE/version > run-version.json
curl -s -H "Authorization: Token $KEY" $BASE/stats   > run-stats.json

# 2) 核对：commit / config_hash 与提交材料一致，且 Full 期间没变过
diff <(python3 -c 'import json;print(json.load(open("run-version.json"))["config_hash"])') \
     <(grep config_hash 提交材料.md | awk '{print $2}')

# 3) 合规：赛事数据用完后 30 天内删除（先 dry-run）
python3 tools/purge.py --db /data/mem.db --user-prefix "aml:" --dry-run
```
