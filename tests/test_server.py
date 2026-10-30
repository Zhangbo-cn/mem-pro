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
# 端到端测试只验证契约：不读开发者 .env（精排/真实 API 不应影响断言）
os.environ.setdefault("DOTENV_DISABLE", "1")
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

    def test_01c_max_results_caps_response_but_never_exceeds_top_k(self):
        """MAX_RESULTS 只能收窄，不能放大；契约要求「返回数量不得超过 top_k」。

        单独起一个设了 MAX_RESULTS=3 的实例（配置在 import 时读取，不能在线改）。
        """
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        db = os.path.join(self.tmp.name, "cap.db")
        env = {**os.environ, "EMBED_MODE": "hash", "DB_PATH": db, "MEMORY_SYSTEM_KEY": KEY,
               "PORT": str(port), "HOST": "127.0.0.1", "STARTUP_CHECK": "0", "MAX_RESULTS": "3"}
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "service", "server.py")],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(base + "/health", timeout=1).read()
                    break
                except Exception:
                    time.sleep(0.1)
            msgs = [{"role": "user", "content": f"alpha beta cap item {i}",
                     "timestamp": 1700000000000 + i * 1000} for i in range(8)]
            self._post(base, "/add", {"request_id": "cap-1", "user_id": "cap-user",
                                      "session_id": "s1", "messages": msgs})
            for top_k, want in ((10, 3), (2, 2)):
                body = self._post(base, "/search", {"query": "alpha beta cap item",
                                                    "user_id": "cap-user", "top_k": top_k})
                self.assertEqual(len(body["data"]), want, f"top_k={top_k}")
            ver = self._get(base, "/version")
            self.assertIn("max_results", json.dumps(ver))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _post(self, base, path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Token {KEY}"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def _get(self, base, path):
        req = urllib.request.Request(base + path,
                                     headers={"Authorization": f"Token {KEY}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def test_01b_version_is_verifiable(self):
        """/version 必须能证明「线上跑的是哪个 commit、哪套配置」——
        平台公榜准入要求「版本一致」，没有这两项就无法自证。"""
        st, raw = self.call("/version")
        self.assertEqual(st, 200)
        body = json.loads(raw)
        self.assertTrue(body["version"].startswith("mem-pro/"))
        self.assertRegex(body["commit"], r"^[0-9a-f]{7,12}(-dirty)?$|^unknown$")
        self.assertRegex(body["config_hash"], r"^[0-9a-f]{16}$")
        # 同一进程内多次请求必须一致（防火墙后不许有随机成分）
        self.assertEqual(json.loads(self.call("/version")[1])["config_hash"], body["config_hash"])
        # 配置指纹必须覆盖影响排序的项
        self.assertIn("rerank", body["retrieval"])
        self.assertIn("temporal_rule", body["retrieval"])

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

    def test_11_search_applies_temporal_rule(self):
        """时态规则要在真实 /search 路径上生效（旧版只在评测用的 hybrid() 里）。"""
        self.add("e2e:tense", "e2e:tense", messages=[
            {"role": "user", "content": "Alice 去年住在慕尼黑。"},
            {"role": "user", "content": "Alice 住在柏林。"},
        ])
        _, now = self.search("Alice 现在住在哪个城市？", "e2e:tense", 5)
        _, plain = self.search("Alice 住在哪个城市？", "e2e:tense", 5)
        past_now = next(d["score"] for d in now["data"] if "去年" in d["content"])
        past_plain = next(d["score"] for d in plain["data"] if "去年" in d["content"])
        self.assertTrue(now["data"][0]["content"].endswith("Alice 住在柏林。"))
        self.assertLess(past_now, past_plain)

    def test_11b_v2_round_unit_with_date_and_speaker_header(self):
        """/v2/*：user+assistant 合成一条，带日期与说话人；/add、/search（v1）行为不变且互不可见。

        平台 Answer 只读 content：没有日期答不了先后题，没有说话人分不清谁说的。
        """
        ts = 1715472000000  # 2024-05-12 00:00 UTC
        msgs = [
            {"role": "user", "content": "I adopted a Maltese named Lumi.", "timestamp": ts},
            {"role": "assistant", "content": "Congrats on Lumi!", "timestamp": ts + 1},
            {"role": "user", "content": "She loves the park.", "timestamp": ts + 60_000},
        ]
        body = {"request_id": "r1", "user_id": "e2e:round", "session_id": "s1", "messages": msgs}
        st, out = self.post("/v2/add", body)
        self.assertEqual(st, 200)
        self.assertEqual(out, {"success": True, "request_id": "r1", "user_id": "e2e:round",
                               "session_id": "s1"})
        _, v1 = self.search("When did I adopt Lumi?", "e2e:round", 10)
        self.assertEqual(v1["data"], [], "v2 写入不能出现在 v1 的检索空间")
        self.add("r1", "e2e:round", messages=msgs)
        _, v1 = self.search("When did I adopt Lumi?", "e2e:round", 10)
        self.assertEqual(len(v1["data"]), 3)
        self.assertIn("I adopted a Maltese named Lumi.", [d["content"] for d in v1["data"]])
        _, body = self.post("/v2/search", {"query": "When did I adopt Lumi?",
                                           "user_id": "e2e:round", "top_k": 10})
        texts = [d["content"] for d in body["data"]]
        self.assertEqual(len(texts), 2, texts)
        self.assertIn("[2024-05-12 00:00] user: I adopted a Maltese named Lumi.\n"
                      "assistant: Congrats on Lumi!", texts)
        self.assertIn("[2024-05-12 00:01] user: She loves the park.", texts)
        st, _ = self.call("/v2/health", key=None)
        self.assertEqual(st, 200)

    def test_12_distinct_image_only_memories_all_returned(self):
        imgs = [PNG.replace("iVBORw0KGgo", f"iVBORw0KGg{c}", 1) for c in "oAB"]
        self.assertEqual(len(set(imgs)), 3)
        for k, img in enumerate(imgs):
            st, _ = self.add(f"e2e:img{k}", "e2e:gallery", messages=[{
                "role": "user", "content": [{"type": "image_url", "image_url": {"url": img}}]}])
            self.assertEqual(st, 200)
        st, body = self.search("photo", "e2e:gallery", 10)
        self.assertEqual(st, 200)
        self.assertEqual(len(body["data"]), 3, "不同图片的纯图记忆被当成重复丢了")

    def test_09b_lenient_parts_never_fail_the_whole_add(self):
        """平台数据里出现不常见的图片子类型 / 空 caption / 未知 part 时，跳过那一段而不是 400。

        规则要求「所有评测任务均执行成功」：一条 Add 被拒 = 那条对话的记忆全丢。
        """
        gif = "data:image/gif;base64,R0lGODlhAQABAAAAACw="
        heic = "data:image/heic;base64,AAAAGGZ0eXBoZWlj"
        cases = {
            "gif": [{"type": "text", "text": "gif photo"},
                    {"type": "image_url", "image_url": {"url": gif}}],
            "heic": [{"type": "text", "text": "heic photo"},
                     {"type": "image_url", "image_url": {"url": heic}}],
            "empty-caption": [{"type": "image_url", "image_url": {"url": PNG}},
                              {"type": "text", "text": ""}],
            "unknown-part": [{"type": "input_audio", "data": "x"},
                             {"type": "text", "text": "still keep this text"}],
        }
        for rid, content in cases.items():
            st, body = self.add(f"lenient:{rid}", "e2e:lenient", "s1",
                                [{"role": "user", "content": content}])
            self.assertEqual(st, 200, f"{rid}: {body}")
        st, body = self.search("still keep this text", "e2e:lenient", 10)
        self.assertEqual(st, 200)
        self.assertTrue(any("still keep" in d["content"] if isinstance(d["content"], str)
                            else any(p.get("text", "").startswith("still keep")
                                     for p in d["content"]) for d in body["data"]))
        # heic 图片原样回传（不解码、不转码）
        st, body = self.search("heic photo", "e2e:lenient", 10)
        urls = [p["image_url"]["url"] for d in body["data"] if isinstance(d["content"], list)
                for p in d["content"] if p.get("type") == "image_url"]
        self.assertIn(heic, urls)
        # 跳过的段要计数，否则问题被静默吞掉
        v = self._get(self.base, "/version")
        self.assertGreaterEqual(v["skipped_parts"]["empty_text"], 1)
        self.assertGreaterEqual(v["skipped_parts"]["unknown_type"], 1)
        # 跳完一无所有才拒：只有一张远程 URL 图
        st, _ = self.add("lenient:remote-only", "e2e:lenient", "s1", [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}])
        self.assertTrue(400 <= st < 500)

    def test_10_unknown_path(self):
        self.assertEqual(self.call("/nope", key=None)[0], 404)
        self.assertEqual(self.call("/nope", {}, key=KEY)[0], 404)


class LogHygieneTest(unittest.TestCase):
    """合规：评测数据只能用于完成当前任务 —— 日志里不许落请求正文。

    规则见 research/06 §八。这里用唯一标记串验证：write/add/search 之后，
    服务端日志里查不到任何消息正文或 query 原文。
    """

    def test_logs_carry_no_request_bodies(self):
        tmp = tempfile.TemporaryDirectory()
        port = free_port()
        log = os.path.join(tmp.name, "server.log")
        marker = "SEKRIT-marker-9f2c1b"
        env = {**os.environ, "EMBED_MODE": "hash", "LOG_JSON": "1",
               "DB_PATH": os.path.join(tmp.name, "log.db"), "MEMORY_SYSTEM_KEY": KEY,
               "PORT": str(port), "HOST": "127.0.0.1", "STARTUP_CHECK": "0"}
        with open(log, "w") as fh:
            proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "service", "server.py")],
                                    env=env, stdout=fh, stderr=fh, text=True)
            try:
                base = f"http://127.0.0.1:{port}"
                for _ in range(150):
                    try:
                        urllib.request.urlopen(base + "/health", timeout=1).read()
                        break
                    except Exception:
                        time.sleep(0.1)
                for path, body in (
                    ("/add", {"request_id": "log-1", "user_id": f"log:{marker}",
                              "session_id": "s", "messages": [
                                  {"role": "user", "content": f"secret body {marker}",
                                   "timestamp": 1704067200000}]}),
                    ("/search", {"query": f"query text {marker}", "user_id": f"log:{marker}",
                                 "top_k": 10}),
                ):
                    req = urllib.request.Request(
                        base + path, data=json.dumps(body).encode(),
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Token {KEY}"})
                    urllib.request.urlopen(req, timeout=30).read()
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            time.sleep(0.3)
        with open(log) as fh:
            text = fh.read()
        tmp.cleanup()
        self.assertIn("/search", text, "日志开关被关掉了，这项检查失效")
        self.assertNotIn(marker, text, "请求正文/query 原文进了日志，违反数据使用规则")
        self.assertNotIn(KEY, text, "Memory System Key 出现在日志里")


if __name__ == "__main__":
    unittest.main(verbosity=2)
