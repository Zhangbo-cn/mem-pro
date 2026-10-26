"""配置体检：一条命令看清「API 到底配没配、能不能用」。

  python3 tools/doctor.py                 # 体检 .env / 环境变量 / 本地依赖
  python3 tools/doctor.py --url https://mem.example.com --key KEY
  python3 tools/doctor.py --init          # 生成 service/.env（不覆盖已有文件）
  python3 tools/doctor.py --json

退出码：0 = 必需项全部通过；1 = 有阻塞项；2 = 仅警告。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "service"
sys.path.insert(0, str(SERVICE))

import dotenv  # noqa: E402

FILES = dotenv.load()

OK, WARN, FAIL = "ok", "warn", "fail"
RESULTS: list[dict] = []


def add(level: str, name: str, detail: str, hint: str = "") -> None:
    RESULTS.append({"level": level, "name": name, "detail": detail, "hint": hint})


def masked(value: str) -> str:
    if not value:
        return "(未设置)"
    if len(value) <= 10:
        return value[0] + "*" * (len(value) - 1)
    return f"{value[:6]}…{value[-2:]}（{len(value)} 字符）"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def probe_http(url: str, timeout: float = 8.0) -> tuple[bool, str, int]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True, f"HTTP {r.status}", r.status
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", e.code
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", 0


def check_python() -> None:
    v = sys.version_info
    if v >= (3, 10):
        add(OK, "Python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        add(FAIL, "Python", f"{v.major}.{v.minor} 过低", "需要 ≥3.10（用了 X | Y 类型语法）")


def check_env_files() -> None:
    if FILES:
        add(OK, ".env 加载", ", ".join(FILES))
    else:
        add(WARN, ".env 加载", "未找到 .env，仅使用当前环境变量",
            "python3 tools/doctor.py --init 可生成 service/.env")


def check_embedding() -> None:
    import embed

    mode = embed.MODE
    add(OK, "Embedding 模式", f"{mode} / model={embed.MODEL} / dim={embed.DIM}")
    if mode not in ("api", "hash"):
        add(FAIL, "Embedding 模式", f"未知模式 {mode!r}", "只能是 api 或 hash")
        return
    if mode == "hash":
        add(WARN, "Embedding API", "未配置（hash 离线模式，仅用于开发/契约测试）",
            "正式评测必须 EMBED_MODE=api + DASHSCOPE_API_KEY（学术榜强制 text-embedding-v4）")
        return
    if not embed.API_KEY:
        add(FAIL, "DASHSCOPE_API_KEY", "缺失", "在 service/.env 写入 DASHSCOPE_API_KEY=sk-...")
        return
    if not embed.API_KEY.startswith("sk-"):
        add(WARN, "DASHSCOPE_API_KEY", f"格式可疑：{masked(embed.API_KEY)}", "百炼 Key 通常以 sk- 开头")
    else:
        add(OK, "DASHSCOPE_API_KEY", masked(embed.API_KEY))
    ok, msg = embed.ping()
    add(OK if ok else FAIL, "DashScope 连通性", msg,
        "" if ok else "检查网络、Key 是否有效、是否已开通百炼/欠费")


def check_storage() -> None:
    import store

    db = Path(store.DB_PATH)
    parent = db.parent if db.parent != Path("") else Path(".")
    try:
        parent.mkdir(parents=True, exist_ok=True)
        store.init()
        ok = store.ping()
        count = store.count() if ok else -1
    except Exception as e:  # noqa: BLE001
        add(FAIL, "SQLite", f"{db} 不可用：{e}", "检查 DB_PATH 目录权限")
        return
    if ok:
        add(OK, "SQLite", f"{db}（现有 {count} 条记忆）")
    else:
        add(FAIL, "SQLite", f"{db} ping 失败", "删除损坏的库或换 DB_PATH")


def check_retrieval() -> None:
    try:
        import retrieve
    except Exception as e:  # noqa: BLE001
        add(WARN, "检索加速", f"retrieve 导入失败：{e}")
        return
    if retrieve._np is not None:
        add(OK, "dense 加速", f"numpy {retrieve._np.__version__}（矩阵内积）")
    elif retrieve._DOT is not None:
        add(WARN, "dense 加速", "纯 Python + math.sumprod（未装 numpy）",
            "装 numpy 可把 6.7k×1024 维单次 Search 从 ~240ms 降到 ~25ms：pip install numpy")
    else:
        add(WARN, "dense 加速", "纯 Python 逐元素点积（既无 numpy 也无 math.sumprod）",
            "建议 Python ≥ 3.12，或安装 numpy")
    add(OK, "检索索引预算",
        f"{retrieve._SEARCH_INDEX_MAX_BYTES // (1024 * 1024)} MiB（RANK_INDEX_MAX_MB）")


def check_auth() -> None:
    key = env("MEMORY_SYSTEM_KEY")
    if not key:
        add(WARN, "鉴权", "未设置 MEMORY_SYSTEM_KEY（服务对所有请求开放）",
            "生产/评测必须设置：openssl rand -hex 24")
    elif len(key) < 16:
        add(FAIL, "鉴权", f"Key 过短（{len(key)} 字符）", "至少 16 字符，推荐 48 字符 hex")
    else:
        add(OK, "鉴权", f"已启用（{masked(key)}）")


def check_port() -> None:
    host, port = env("HOST", "0.0.0.0"), int(env("PORT", "8080"))
    bind_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((bind_host, port))
            add(OK, "监听端口", f"{host}:{port} 可用")
        except OSError as e:
            add(WARN, "监听端口", f"{host}:{port} 占用或不可绑定（{e}）",
                "若服务已在运行则正常；否则换 PORT")


def check_caption() -> None:
    try:
        import caption
    except Exception as e:  # noqa: BLE001
        add(FAIL, "图片描述", f"caption 导入失败：{e}")
        return
    ok, msg = caption.check()
    if not caption.enabled():
        add(OK, "图片描述", msg + "；只依赖平台 caption 文本",
            "纯图消息无法被文本检索命中时，可设 CAPTION_MODE=openai（仅 gpt-4o-mini）")
        return
    add(OK if ok else FAIL, "图片描述", msg)


def check_pool() -> None:
    import embed
    import http_pool
    import retrieve

    size = http_pool.pool_size()
    need = embed.CONCURRENCY + (retrieve.CONCURRENCY_RERANK if retrieve.RERANK_MODE == "http" else 0)
    add(OK if size >= need else WARN, "连接池",
        f"HTTP_POOL_SIZE={size}，需要 ≥ embedding {embed.CONCURRENCY}"
        f"{f' + 精排 {retrieve.CONCURRENCY_RERANK}' if retrieve.RERANK_MODE == 'http' else ''}",
        "" if size >= need else "池小于并发时请求会在池上排队，表现为 p95 变长")


def check_capacity() -> None:
    """并发自检：闸门、backlog、申报口径。

    平台「申报即承诺」，所以本地只能检查参数是否自相矛盾，实测必须靠 tools/search_load.py。
    """
    import embed

    inflight = int(env("MAX_INFLIGHT") or 64)
    backlog = int(env("LISTEN_BACKLOG") or 512)
    conc = embed.CONCURRENCY
    detail = f"在途上限 {inflight}，embedding 并发闸门 {conc}，listen backlog {backlog}"
    if backlog < 128:
        add(WARN, "容量", detail + "；backlog 过小，高并发建连会丢 SYN、p95 飙到秒级",
            "设 LISTEN_BACKLOG=512")
        return
    if embed.MODE == "api" and conc > 32:
        add(WARN, "容量", detail + "；api 模式下并发闸门 >32 容易撞 DashScope QPM",
            "先按百炼控制台 RPM 定 EMBED_CONCURRENCY，再用 tools/search_load.py 实测")
        return
    if conc > inflight:
        add(WARN, "容量", detail + "；闸门大于在途上限，说明 MAX_INFLIGHT 会先拒绝请求",
            "让 MAX_INFLIGHT ≥ EMBED_CONCURRENCY")
        return
    add(OK, "容量", detail + "；申报值请用 tools/search_load.py --sweep 实测后取 60–70%")


def check_rerank() -> None:
    import retrieve

    if retrieve.RERANK_MODE != "http":
        add(OK, "Reranker", "未启用（dense+BM25 线性融合 + 时态规则）",
            "代理榜实测：开 gte-rerank-v2 后 MRR 0.761→0.808，代价是 p50 多约 0.7 s、吞吐降约 15%。"
            "填 RERANK_MODE/KIND/URL/MODEL/API_KEY 即可启用（见 service/.env.example）")
        return
    if not retrieve.RERANK_URL:
        add(FAIL, "Reranker", "RERANK_MODE=http 但 RERANK_URL 为空")
        return
    headers = {"Content-Type": "application/json"}
    if retrieve.RERANK_API_KEY:
        headers["Authorization"] = f"Bearer {retrieve.RERANK_API_KEY}"
    if retrieve.RERANK_KIND == "dashscope":
        payload = {"model": retrieve.RERANK_MODEL or "gte-rerank-v2",
                   "input": {"query": "ping", "documents": ["pong"]},
                   "parameters": {"return_documents": False}}
    else:
        payload = {"query": "ping", "documents": ["pong"], "top_n": 1}
        if retrieve.RERANK_MODEL:
            payload["model"] = retrieve.RERANK_MODEL
    req = urllib.request.Request(retrieve.RERANK_URL,
                                 data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=retrieve.RERANK_TIMEOUT) as r:
            body = json.loads(r.read().decode())
        ok = isinstance(body, (dict, list))
        add(OK if ok else FAIL, "Reranker",
            f"{retrieve.RERANK_URL} 可用（kind={retrieve.RERANK_KIND}）")
    except Exception as e:  # noqa: BLE001
        add(FAIL, "Reranker", f"{retrieve.RERANK_URL} 不可用：{type(e).__name__}: {e}",
            "服务会自动降级为融合序，但精排收益会丢失")


def check_target(url: str, key: str) -> None:
    url = url.rstrip("/")
    ok, msg, _ = probe_http(url + "/health")
    add(OK if ok else FAIL, "目标 /health", f"{url}/health → {msg}",
        "" if ok else "确认 DNS/HTTPS/反代/防火墙")
    if not key:
        add(WARN, "目标鉴权", "未提供 --key，跳过鉴权探测")
        return
    req = urllib.request.Request(url + "/stats", headers={"Authorization": f"Token {key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read().decode())
        add(OK, "目标鉴权", f"/stats 通过，记忆数 {body.get('memories')}")
    except urllib.error.HTTPError as e:
        add(FAIL, "目标鉴权", f"/stats HTTP {e.code}", "确认 Memory System Key 与提交给平台的一致")
    except Exception as e:  # noqa: BLE001
        add(FAIL, "目标鉴权", f"/stats 失败：{type(e).__name__}: {e}")


def do_init() -> int:
    target = SERVICE / ".env"
    if target.exists():
        print(f"已存在 {target}，不覆盖。")
        return 0
    key = secrets.token_hex(24)
    content = f"""# mem-pro 本地配置（已被 .gitignore 忽略，切勿提交）
# 开发用 hash 模式；正式评测改成 api 并填入百炼 Key
EMBED_MODE=hash
# EMBED_MODE=api
DASHSCOPE_API_KEY=
EMBED_MODEL=text-embedding-v4
EMBED_DIM=1024

HOST=0.0.0.0
PORT=8080
DB_PATH=./mem.db

# 平台调你接口用的鉴权 key（已随机生成）
MEMORY_SYSTEM_KEY={key}

# 检索索引：单条 Search 的复用缓存上限（MiB）
RANK_INDEX_MAX_MB=64

# 可选精排
RERANK_MODE=none
# RERANK_MODE=http
# RERANK_URL=http://127.0.0.1:8010/rerank
# RERANK_MODEL=BAAI/bge-reranker-v2-m3
# RERANK_API_KEY=
RERANK_TOP=30
MAX_INFLIGHT=64
# 同时发往 DashScope 的请求上限（按百炼 RPM 配额设）
EMBED_CONCURRENCY=16
# TCP listen backlog；默认 5 时高并发会丢 SYN
LISTEN_BACKLOG=512
MAX_IMAGES=6
LOG_JSON=1
"""
    target.write_text(content, encoding="utf-8")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"已生成 {target}（权限 600）。请填入 DASHSCOPE_API_KEY 后再跑 doctor。")
    print(f"Memory System Key: {key}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="mem-pro 配置体检")
    ap.add_argument("--url", default="", help="可选：检查已部署服务的 /health 与 /stats")
    ap.add_argument("--key", default=env("MEMORY_SYSTEM_KEY"), help="部署服务的鉴权 key")
    ap.add_argument("--init", action="store_true", help="生成 service/.env")
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = ap.parse_args()

    if args.init:
        return do_init()

    check_python()
    check_env_files()
    check_embedding()
    check_storage()
    check_retrieval()
    check_auth()
    check_port()
    check_rerank()
    check_caption()
    check_pool()
    check_capacity()
    if args.url:
        check_target(args.url, args.key)

    if args.json:
        print(json.dumps({"files": FILES, "results": RESULTS}, ensure_ascii=False, indent=2))
    else:
        icon = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}
        print("\nmem-pro 配置体检\n" + "─" * 64)
        for r in RESULTS:
            print(f"{icon[r['level']]} {r['name']}: {r['detail']}")
            if r["hint"]:
                print(f"   ↳ {r['hint']}")
        print("─" * 64)
        bad = sum(r["level"] == FAIL for r in RESULTS)
        warn = sum(r["level"] == WARN for r in RESULTS)
        print(f"通过 {len(RESULTS) - bad - warn} / 警告 {warn} / 阻塞 {bad}")
        if bad:
            print("\n结论：还不能用于正式评测，请先处理 ❌ 项。")
        elif any(r["name"].startswith("Embedding API") and r["level"] == WARN for r in RESULTS):
            print("\n结论：本地开发可用；正式评测前需切到 api 模式并配置百炼 Key。")
        else:
            print("\n结论：配置完整。")

    if any(r["level"] == FAIL for r in RESULTS):
        return 1
    if any(r["level"] == WARN for r in RESULTS):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
