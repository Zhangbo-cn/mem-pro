"""文档 ↔ 运行时一致性检查：提交材料里写的数字必须等于代码默认值。

审核时 SUBMISSION.md 会被当成证据；文档写 1e-3、代码是 0.0003 这种不一致
（2026-10-26 实际发生过）会被视为不可复现。本脚本读默认配置（不带 .env），
在 SUBMISSION.md / README.md 里查找应当出现的字符串，缺一条就非零退出。

  python3 tools/doc_check.py          # make check 也会跑
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE = os.path.join(ROOT, "service")


def runtime() -> dict:
    """在干净环境的子进程里读默认值（避免本机 .env / 环境变量污染）。"""
    code = r"""
import json, server, retrieve, budget
print(json.dumps({
  "dense": retrieve.DENSE_WEIGHT, "sparse": retrieve.SPARSE_WEIGHT,
  "dedup_cos": retrieve.DEDUP_COS, "temporal_weight": retrieve.TEMPORAL_WEIGHT,
  "rerank_mode": retrieve.RERANK_MODE, "rerank_model": retrieve.RERANK_MODEL,
  "rerank_top": retrieve.RERANK_TOP, "rerank_tol": retrieve.RERANK_TOL,
  "max_results": server.MAX_RESULTS, "max_images": server.MAX_IMAGES,
  "token_budget": budget.TOKEN_BUDGET, "max_inflight": server.MAX_INFLIGHT,
}))
"""
    keep = {"PATH", "HOME", "LANG", "PYTHONPATH"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(EMBED_MODE="hash", STARTUP_CHECK="0", DOTENV_DISABLE="1",
               DB_PATH=os.path.join("/tmp", f"doc-check-{os.getpid()}.db"))
    out = subprocess.run([sys.executable, "-c", code], cwd=SERVICE, env=env,
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        sys.exit(out.stderr)
    import json
    return json.loads(out.stdout.strip().splitlines()[-1])


def submission_env(name: str, default):
    """读本次提交的 service/.env 覆盖值（提交材料必须写部署实际值，而不是代码默认值）。"""
    try:
        for line in open(os.path.join(ROOT, "service", ".env"), encoding="utf-8"):
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return default
    return default


def submission_rerank_on() -> bool:
    """提交配置（service/.env）是否启用精排：文档必须与实际提交的配置一致。"""
    env_file = os.path.join(ROOT, "service", ".env")
    try:
        for line in open(env_file, encoding="utf-8"):
            line = line.strip()
            if line.startswith("RERANK_MODE="):
                return line.split("=", 1)[1].strip().lower() == "http"
    except OSError:
        return False
    return False


def test_count() -> int:
    n = 0
    for f in os.listdir(os.path.join(ROOT, "tests")):
        if f.endswith(".py"):
            n += len(re.findall(r"^\s+def test_", open(os.path.join(ROOT, "tests", f),
                                                         encoding="utf-8").read(), re.M))
    return n


def main() -> int:
    rt = runtime()
    sub = open(os.path.join(ROOT, "SUBMISSION.md"), encoding="utf-8").read()
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    n = test_count()
    tok = f"{rt['token_budget']:,}"
    checks = [
        ("SUBMISSION 融合权重", sub, f"α={rt['dense']:g} / β={rt['sparse']:g}"),
        ("SUBMISSION 去重阈值", sub, f"余弦 ≥ {rt['dedup_cos']:g}"),
        ("SUBMISSION 时态权重", sub, f"扣 {rt['temporal_weight']:g}"),
        ("SUBMISSION 精排容差", sub, f"分差 < {rt['rerank_tol']:g}"),
        ("SUBMISSION 精排模型", sub, rt["rerank_model"]),
        ("SUBMISSION 精排状态",
         sub,
         ("RERANK_MODE=http" if submission_rerank_on() else "代码默认值仍为 `RERANK_MODE=none`")),
        ("SUBMISSION 返回条数", sub, f"MAX_RESULTS={rt['max_results']}"),
        # 冻结部署的实际值（线上 service/.env），与代码默认值不同：代码默认 MAX_IMAGES=6，
        # 本次提交按 2 Mbit 带宽取 3 并开启 IMG_MAX_SIDE 缩图，文档必须写实际值。
        ("SUBMISSION 图片上限", sub, "图片 ≤ 3 张"),
        ("SUBMISSION 缩图说明", sub, "IMG_MAX_SIDE=1024"),
        ("SUBMISSION token 预算", sub, f"token ≤ {tok}"),
        ("SUBMISSION 在途上限", sub, f"在途请求上限 {rt['max_inflight']}"),
        ("SUBMISSION 测试数", sub, f"测试 {n} 项"),
        ("README 测试数", readme, f"{n} 项"),
        ("README 返回条数", readme, f"MAX_RESULTS={rt['max_results']}"),
    ]
    bad = 0
    for name, doc, needle in checks:
        ok = needle in doc
        bad += not ok
        print(f"{'✅' if ok else '❌'} {name}: 应包含 {needle!r}")
    stale = [s for s in ("1e-3 容差", "created_at asc", "保留更早者", "p50 ≈ 1.2 s")
             if s in sub or s in readme]
    for s in stale:
        print(f"❌ 过期表述仍在文档里: {s!r}")
    bad += len(stale)
    print(f"\n{'全部一致' if not bad else f'{bad} 处不一致'}（测试 {n} 项）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
