"""本地精排服务（TEI 兼容 /rerank），默认 bge-reranker-v2-m3。

用法：
  .venv/bin/python tools/rerank_server.py                     # 默认 models/bge-reranker-v2-m3:8010
  RERANK_MODEL_DIR=models/xxx PORT=8010 .venv/bin/python tools/rerank_server.py

接口（与 HuggingFace TEI 一致，便于换模型/换服务）：
  POST /rerank {"query": "...", "documents": ["...", ...], "top_n": 10}
    → [{"index": 3, "score": 0.91}, ...]
  GET  /health → {"status":"ok"}
"""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

MODEL_DIR = os.environ.get("RERANK_MODEL_DIR", "models/bge-reranker-v2-m3")
PORT = int(os.environ.get("PORT", "8010"))
BATCH = int(os.environ.get("RERANK_BATCH", "16"))
MAX_LEN = int(os.environ.get("RERANK_MAXLEN", "512"))
THREADS = int(os.environ.get("TORCH_THREADS", "8"))

torch.set_num_threads(THREADS)
torch.set_grad_enabled(False)

print(f"[rerank] loading {MODEL_DIR} ...", flush=True)
t0 = time.time()
TOK = AutoTokenizer.from_pretrained(MODEL_DIR)
MODEL = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR, dtype=torch.float32).eval()
print(f"[rerank] ready in {time.time()-t0:.1f}s  threads={THREADS} batch={BATCH}", flush=True)


def score(query: str, docs: list[str]) -> list[float]:
    out: list[float] = []
    for i in range(0, len(docs), BATCH):
        pairs = [[query, d] for d in docs[i : i + BATCH]]
        enc = TOK(pairs, padding=True, truncation=True, max_length=MAX_LEN,
                  return_tensors="pt")
        logits = MODEL(**enc).logits.view(-1)
        out.extend(torch.sigmoid(logits).tolist())
    return out


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, {"status": "ok", "model": MODEL_DIR})
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n).decode())
        except Exception:
            return self._send(422, {"detail": "bad json"})
        q, docs = req.get("query"), req.get("documents") or []
        if not isinstance(q, str) or not q or not docs:
            return self._send(422, {"detail": "query/documents required"})
        t = time.time()
        sc = score(q, docs)
        order = sorted(range(len(docs)), key=lambda i: (-sc[i], i))
        top_n = int(req.get("top_n") or len(docs))
        res = [{"index": i, "score": float(sc[i])} for i in order[:top_n]]
        self._send(200, {"results": res, "ms": round((time.time() - t) * 1000, 1)})


if __name__ == "__main__":
    print(f"[rerank] listen 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
