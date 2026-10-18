"""端到端测试：真实启动 HTTP 服务（hash 模式 + 临时库），按官方契约打。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "test-key-0123456789"
PNG = ("data:image/png;base64," + base64.b64encode(
    bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                  "890000000a4944415478da6300010000050001a5f645400000000049454e44ae426082")
).decode())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = {
            **os.environ,
            "EMBED_MODE": "hash",
            "DB_PATH": os.path.join(cls.tmp.name, "e2e.db"),
            "MEMORY_SYSTEM_KEY": KEY,
            "PORT": str(cls.port),
            "HOST": "127.0.0.1",
            "STARTUP_CHECK": "0",
        }
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "service", "server.py")],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                out, err = cls.proc.communicate()
                raise RuntimeError(f"服务启动失败\nstdout={out}\nstderr={err}")
            try:
                urllib.request.urlopen(cls.base + "/health", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        else:
            cls.proc.kill()
            raise RuntimeError("服务 15s 内未就绪")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.tmp.cleanup()

    # ---------- helpers ----------
    def call(self, path, body=None, key=KEY, method=None):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Token {key}"
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers,
            method=method or ("POST" if body is not None else "GET"),
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, raw
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(self, path, body, key=KEY):
        st, raw = self.call(path, body, key)
        return st, json.loads(raw.decode())

    def add(self, rid, user, session="s1", messages=None):
        return self.post("/add", {
            "request_id": rid, "user_id": user, "session_id": session,
            "messages": messages or [{"role": "user", "content": "Alice lives in Berlin.",
                                      "timestamp": 1704067200000}],
        })

    def search(self, query, user, top_k=100):
        return self.post("/search", {"query": query, "user_id": user, "top_k": top_k})

    # ---------- tests ----------
    def test_01_health_public(self):
        st, raw = self.call("/health", key=None)
        self.assertEqual(st, 200)
        body = json.loads(raw)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["db"], "ok")
        self.assertNotIn(KEY, raw.decode())

    def test_02_auth(self):
        self.assertEqual(self.call("/stats", key=None)[0], 401)
        self.assertEqual(self.call("/stats", key="wrong")[0], 401)
        self.assertEqual(self.call("/stats", key=KEY)[0], 200)
        # 三种官方鉴权头都接受
        for header in (f"Bearer {KEY}", f"Token {KEY}"):
            req = urllib.request.Request(self.base + "/stats",
                                         headers={"Authorization": header})
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 200)
        req = urllib.request.Request(self.base + "/stats", headers={"X-Api-Key": KEY})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)

    def test_03_add_echo_and_idempotency(self):
        body = {"request_id": "e2e:idem", "user_id": "e2e:u1", "session_id": "e2e:s1",
                "messages": [{"role": "user", "content": "Alice lives in Berlin.",
                              "timestamp": 1704067200000}]}
        st, first = self.post("/add", body)
        self.assertEqual(st, 200)
        self.assertIs(first["success"], True)
        for k in ("request_id", "user_id", "session_id"):
            self.assertEqual(first[k], body[k])

        st, raw_before = self.call("/stats")
        before = json.loads(raw_before)["memories"]
        st, second = self.post("/add", body)
        self.assertEqual(st, 200)
        self.assertEqual(second, first)
        st, raw_after = self.call("/stats")
        self.assertEqual(json.loads(raw_after)["memories"], before, "重放不应新增记忆")

    def test_04_concurrent_idempotency(self):
        body = {"request_id": "e2e:race", "user_id": "e2e:race", "session_id": "s",
                "messages": [{"role": "user", "content": "race condition memory"}]}
        before = json.loads(self.call("/stats")[1])["memories"]
        results = []
        errors = []

        def worker():
            try:
                results.append(self.post("/add", body))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(st == 200 for st, _ in results))
        first = results[0][1]
        self.assertTrue(all(resp == first for _, resp in results))
        st, raw = self.call("/stats")
        self.assertEqual(json.loads(raw)["memories"], before + 1, "并发重放只应写一次")

    def test_05_search_and_isolation(self):
        self.add("e2e:r1", "e2e:alice")
        st, body = self.search("Where does Alice live?", "e2e:alice", 5)
        self.assertEqual(st, 200)
        self.assertIn("data", body)
        self.assertNotIn("items", body)
        self.assertTrue(body["data"])
        item = body["data"][0]
        self.assertTrue(item["id"])
        self.assertTrue(item["content"])
        self.assertIsInstance(item["score"], (int, float))
        self.assertLessEqual(len(body["data"]), 5)

        st, body = self.search("Where does Alice live?", "e2e:bob")
        self.assertEqual(body, {"data": []})

    def test_06_empty_and_topk(self):
        st, body = self.search("nothing matches", "e2e:nobody", 10)
        self.assertEqual(st, 200)
        self.assertEqual(body, {"data": []})

        self.add("e2e:many", "e2e:many", messages=[
            {"role": "user", "content": f"note {i} about topic {i}"} for i in range(30)
        ])
        for k in (1, 3, 10, 100):
            st, body = self.search("note topic", "e2e:many", k)
            self.assertEqual(st, 200)
            self.assertLessEqual(len(body["data"]), k)

    def test_07_multimodal(self):
        st, _ = self.add("e2e:mm", "e2e:mm", messages=[{
            "role": "user",
            "content": [{"type": "text", "text": "receipt from cafe on 2026-03-02"},
                        {"type": "image_url", "image_url": {"url": PNG}}],
            "timestamp": 1772409600000,
        }])
        self.assertEqual(st, 200)
        st, body = self.search([{"type": "text", "text": "cafe receipt"}], "e2e:mm", 5)
        self.assertEqual(st, 200)
        self.assertTrue(body["data"])
        content = body["data"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertTrue(any(p["type"] == "image_url" for p in content))

    def test_08_rejects_bad_requests(self):
        base = {"request_id": "e2e:bad", "user_id": "e2e:bad", "session_id": "s"}
        cases = [
            ("/add", {**base, "messages": [{"role": "user", "content": ""}]}),
            ("/add", {**base, "messages": [{"role": "tool", "content": "x"}]}),
            ("/add", {**base, "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}]}),
            ("/add", {**base, "messages": [{"role": "user", "content": "x"}],
                      "request_id": ""}),
            ("/search", {"query": "x", "user_id": "u"}),
            ("/search", {"query": "x", "user_id": "u", "top_k": 0}),
            ("/search", {"query": "x", "user_id": "u", "top_k": True}),
            ("/search", {"query": "", "user_id": "u", "top_k": 5}),
        ]
        for path, payload in cases:
            st, _ = self.post(path, payload)
            self.assertTrue(400 <= st < 500, f"{path} {payload} → {st}")

    def test_09_determinism(self):
        self.add("e2e:det", "e2e:det", messages=[
            {"role": "user", "content": "The meeting moved to Thursday."},
            {"role": "assistant", "content": "Noted, Thursday works."},
        ])
        outs = [self.call("/search", {"query": "When is the meeting?",
                                      "user_id": "e2e:det", "top_k": 20})[1]
                for _ in range(3)]
        self.assertTrue(outs[0] == outs[1] == outs[2], "Search 响应不确定")

    def test_10_unknown_path(self):
        self.assertEqual(self.call("/nope", key=None)[0], 404)
        self.assertEqual(self.call("/nope", {}, key=KEY)[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
