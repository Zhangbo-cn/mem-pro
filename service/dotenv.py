"""极简 .env 加载器（纯标准库，无 python-dotenv 依赖）。

查找顺序（先找到先加载，已存在的环境变量永不覆盖）：
  1) $ENV_FILE（若显式指定）
  2) <repo>/.env
  3) <repo>/service/.env

`DOTENV_DISABLE=1` 时完全不读（tools/doc_check.py、指纹测试用来读纯默认值）。
支持 `KEY=value`、`export KEY=value`、单/双引号、`#` 行注释。
服务与工具在读取配置前调用 `load()`，因此本地开发只需复制
`service/.env.example` 为 `service/.env`，无需手动 export。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LOADED = False


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].strip()
    if "=" not in line:
        return None
    key, val = line.split("=", 1)
    key, val = key.strip(), val.strip()
    if not key:
        return None
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return key, val


def load(paths: list[str | os.PathLike] | None = None, override: bool = False) -> list[str]:
    """加载 .env；返回实际读取的文件列表。重复调用只生效一次。"""
    global _LOADED
    if _LOADED and not override and paths is None:
        return []
    if os.environ.get("DOTENV_DISABLE") == "1" and paths is None:   # 测试/文档核对要读纯默认值
        _LOADED = True
        return []
    candidates: list[Path] = []
    if paths:
        candidates = [Path(p) for p in paths]
    else:
        env_file = os.environ.get("ENV_FILE", "").strip()
        if env_file:
            candidates.append(Path(env_file))
        candidates += [ROOT / ".env", ROOT / "service" / ".env"]

    used: list[str] = []
    for path in candidates:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            parsed = _parse_line(line)
            if not parsed:
                continue
            key, val = parsed
            if override or key not in os.environ:
                os.environ[key] = val
        used.append(str(path))
    if not paths:
        _LOADED = True
    return used
