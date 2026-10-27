"""纯单元测试：预算裁剪 / 混合检索 / 存储 / embedding（hash 模式）。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "service"))

os.environ.setdefault("EMBED_MODE", "hash")

import budget  # noqa: E402
import embed  # noqa: E402
import retrieve  # noqa: E402
import store  # noqa: E402


class BudgetTest(unittest.TestCase):
    def test_est_tokens(self):
        self.assertGreaterEqual(budget.est_tokens("你好世界"), 4)
        self.assertGreaterEqual(budget.est_tokens("hello world"), 2)

    def test_clip_respects_max_items(self):
        items = [{"text": f"doc {i}", "images": []} for i in range(10)]
        self.assertEqual(len(budget.clip(items, 3)), 3)

    def test_clip_image_quota(self):
        img = "data:image/png;base64," + "A" * 400
        items = [{"text": f"doc {i}", "images": [img]} for i in range(20)]
        got = budget.clip(items, 100, max_images=2)
        self.assertEqual(sum(len(x["images"]) for x in got), 2)

    def test_clip_drops_images_before_evidence(self):
        img = "data:image/png;base64," + "A" * 400
        old_tok, old_bytes, old_img = budget.TOKEN_BUDGET, budget.BYTE_BUDGET, budget.IMG_TOKEN_COST
        try:
            budget.TOKEN_BUDGET, budget.BYTE_BUDGET, budget.IMG_TOKEN_COST = 3000, 10**9, 4000
            got = budget.clip([{"text": "only evidence", "images": [img]}], 5)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["images"], [])
            self.assertEqual(got[0]["text"], "only evidence")
        finally:
            budget.TOKEN_BUDGET, budget.BYTE_BUDGET, budget.IMG_TOKEN_COST = old_tok, old_bytes, old_img

    def test_clip_truncates_oversized_first_item(self):
        old = budget.TOKEN_BUDGET
        try:
            budget.TOKEN_BUDGET = 200
            got = budget.clip([{"text": "x" * 10_000, "images": []}], 5)
            self.assertEqual(len(got), 1)
            self.assertLessEqual(budget.est_tokens(got[0]["text"]), 220)
        finally:
            budget.TOKEN_BUDGET = old

    def test_clip_byte_budget_uses_base64_wire_size(self):
        img = "data:image/png;base64," + "A" * 4000
        old = budget.BYTE_BUDGET
        try:
            budget.BYTE_BUDGET = 1000
            got = budget.clip([{"text": "t", "images": [img]}], 5)
            self.assertEqual(got[0]["images"], [])
        finally:
            budget.BYTE_BUDGET = old


class RetrieveTest(unittest.TestCase):
    def _docs(self, texts):
        vecs = embed.embed(texts)
        return [
            {"id": f"d{i}", "text": t, "images": [], "created_at": "2026-01-01T00:00:00Z",
             "seq": i, "vec": embed.pack(v)}
            for i, (t, v) in enumerate(zip(texts, vecs))
        ]

    def test_bm25_prefers_keyword(self):
        docs = self._docs(["Alice lives in Berlin", "Bob likes jazz", "weather is nice"])
        got = retrieve.run(embed.embed(["Berlin"])[0], "Berlin", docs)
        self.assertEqual(got[0][0], 0)

    def test_dedup_near_duplicates(self):
        docs = self._docs(["Alice lives in Berlin.", "Bob likes jazz.", "totally different"])
        ranked = [(0, 1.0), (1, 0.9), (2, 0.8)]
        got = retrieve.dedup(ranked, docs)
        self.assertEqual(len(got), 3)  # 语义不同，不应误删

    def test_deterministic(self):
        docs = self._docs(["a b c", "b c d", "c d e"])
        q = embed.embed(["b c"])[0]
        self.assertEqual(retrieve.run(q, "b c", docs), retrieve.run(q, "b c", docs))

    def test_numpy_and_python_paths_agree(self):
        docs = self._docs([f"doc number {i}" for i in range(200)])
        q = embed.embed(["doc number 7"])[0]
        fast = retrieve.dense_scores(q, docs)
        saved = retrieve._np
        try:
            retrieve._np = None
            slow = retrieve.dense_scores(q, docs)
        finally:
            retrieve._np = saved
        for a, b in zip(fast, slow):
            self.assertAlmostEqual(a, b, places=5)

    def test_cached_search_index_and_limited_order_match_exact_prefix(self):
        docs = self._docs([f"unique memory record {i} contains token {i % 17}" for i in range(260)])
        q = embed.embed(["record 37"])[0]
        exact = retrieve.run(q, "record 37", docs)
        key = ("unit-index", "u-" + str(id(self)))
        first_stats = {}
        limited = retrieve.run(q, "record 37", docs, limit=11, cache_key=key, stats=first_stats)
        self.assertEqual(limited, exact[:11])
        self.assertFalse(first_stats["index_cache_hit"])

        hit_stats = {}
        cached = retrieve.run(q, "record 37", docs, limit=11, cache_key=key, stats=hit_stats)
        self.assertEqual(cached, exact[:11])
        self.assertTrue(hit_stats["index_cache_hit"])
        cached_index = retrieve._SEARCH_INDEX[key]
        for query in ("record 37", "token record record", "unique memory", "missing word"):
            expected_sparse = retrieve.bm25_scores(query, docs)
            cached_sparse = retrieve.bm25_scores(query, docs, cached_index)
            for expected_score, cached_score in zip(expected_sparse, cached_sparse):
                self.assertAlmostEqual(cached_score, expected_score, places=12)
        cached_dense = retrieve.dense_scores(q, docs, cached_index)
        for expected_score, got_score in zip(retrieve.dense_scores(q, docs), cached_dense):
            self.assertAlmostEqual(got_score, expected_score, places=5)

        grown = docs + self._docs(["new memory appended at the end"])
        grown[-1]["id"] = "new-last-id"
        stale_stats = {}
        got = retrieve.run(q, "record 37", grown, limit=11, cache_key=key, stats=stale_stats)
        expected = retrieve.run(q, "record 37", grown)[:11]
        self.assertEqual(got, expected)
        self.assertFalse(stale_stats["index_cache_hit"])
        # Append-only growth reuses the cached index instead of rebuilding it from scratch.
        self.assertTrue(stale_stats["index_extended"])
        self.assertEqual(retrieve._SEARCH_INDEX[key].count, len(grown))
        for query in ("record 37", "new memory"):
            expected_sparse = retrieve.bm25_scores(query, grown)
            cached_sparse = retrieve.bm25_scores(query, grown, retrieve._SEARCH_INDEX[key])
            for expected_score, cached_score in zip(expected_sparse, cached_sparse):
                self.assertAlmostEqual(cached_score, expected_score, places=12)

        # Rewriting an existing memory is not append-only: the index must be rebuilt.
        replaced = [dict(d) for d in grown]
        replaced[3]["text"] = "rewritten third record about tokens"
        rewrite_stats = {}
        got = retrieve.run(q, "record 37", replaced, limit=11, cache_key=key, stats=rewrite_stats)
        self.assertNotIn("index_extended", rewrite_stats)
        self.assertEqual(got, retrieve.run(q, "record 37", replaced)[:11])

        saved_max = retrieve._SEARCH_INDEX_MAX_BYTES
        try:
            retrieve._SEARCH_INDEX_MAX_BYTES = 1
            oversized_key = ("unit-index-cap", key[1])
            no_cache_stats = {}
            no_cache = retrieve.run(q, "record 37", docs, limit=11,
                                    cache_key=oversized_key, stats=no_cache_stats)
            self.assertEqual(no_cache, exact[:11])
            self.assertFalse(no_cache_stats["index_cache_hit"])
            self.assertNotIn(oversized_key, retrieve._SEARCH_INDEX)
        finally:
            retrieve._SEARCH_INDEX_MAX_BYTES = saved_max

    def test_scores_identical_with_and_without_index_cache(self):
        """缓存命中/淘汰不影响 score：否则同一 query 响应字节会变（复现风险）。"""
        docs = self._docs([f"record {i} about topic {i % 7} and item {i % 3}" for i in range(200)])
        q = embed.embed(["topic 3 item 1"])[0]
        plain = retrieve.run(q, "topic 3 item 1", docs, limit=20)
        key = ("unit-score-identity", str(id(self)))
        cached = retrieve.run(q, "topic 3 item 1", docs, limit=20, cache_key=key)
        again = retrieve.run(q, "topic 3 item 1", docs, limit=20, cache_key=key)
        self.assertEqual(plain, cached)
        self.assertEqual(cached, again)
        saved = retrieve._np
        try:
            retrieve._np = None
            py = retrieve.run(q, "topic 3 item 1", docs, limit=20)
        finally:
            retrieve._np = saved
        self.assertEqual(py, plain)

    def test_sparse_budget_exhaustion_keeps_vector_path_exact(self):
        docs = self._docs([f"semi structured log line {i} keyword{i % 5}" for i in range(60)])
        q = embed.embed(["log line 3"])[0]
        exact = retrieve.run(q, "log line 3", docs)
        key = ("unit-index-sparse-cap", str(id(self)))
        saved_max = retrieve._SEARCH_INDEX_MAX_BYTES
        try:
            # Room for vectors, not for the posting lists: BM25 falls back, dense stays indexed.
            retrieve._SEARCH_INDEX_MAX_BYTES = retrieve._estimate_search_index_bytes(docs) + 64
            got = retrieve.run(q, "log line 3", docs, limit=7, cache_key=key)
            self.assertEqual(got, exact[:7])
            index = retrieve._SEARCH_INDEX[key]
            self.assertIsNone(index.postings)
            if retrieve._np is not None or retrieve._DOT is not None:  # 3.10 无 numpy 时无向量索引
                self.assertTrue(index.matrix is not None or index.rows is not None)
            indexed_dense = retrieve.dense_scores(q, docs, index)
            for expected_score, got_score in zip(retrieve.dense_scores(q, docs), indexed_dense):
                self.assertAlmostEqual(got_score, expected_score, places=5)
        finally:
            retrieve._SEARCH_INDEX_MAX_BYTES = saved_max
            retrieve._SEARCH_INDEX.pop(key, None)


class EmbedTest(unittest.TestCase):
    def test_hash_deterministic_and_normalized(self):
        a = embed.embed(["Alice lives in Berlin"])[0]
        b = embed.embed(["Alice lives in Berlin"])[0]
        self.assertEqual(a, b)
        self.assertAlmostEqual(sum(x * x for x in a), 1.0, places=6)

    def test_cache_returns_consistent_vector(self):
        first = embed.embed(["cache me"])[0]
        second = embed.embed(["cache me"])[0]
        self.assertEqual(first, second)

    def test_api_retries_transient_read_errors(self):
        """实测：10 条 ×1024 维的响应约 200 KB，连接中途被掐断会抛 IncompleteRead。
        这类瞬时故障必须重试，否则整个 Add 变 503、白烧平台的重试次数。"""
        import http.client

        payload = b'{"data": [{"index": 0, "embedding": [1.0, 0.0]}]}'
        calls = {"n": 0}

        def fake_post(url, body, headers, timeout, size=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise http.client.IncompleteRead(b"{}", 18709)
            if calls["n"] == 2:
                raise TimeoutError("read timed out")
            return payload

        saved = (embed.API_KEY, embed._STATS.copy(), embed.http_pool.post)
        embed.API_KEY, embed.http_pool.post = "sk-test", fake_post
        try:
            self.assertEqual(embed._call_api_unlocked(["hello"])[0], [1.0, 0.0])
            self.assertGreaterEqual(calls["n"], 3)
            self.assertGreaterEqual(embed._STATS["api_net"], 2)
        finally:
            embed.API_KEY, embed.http_pool.post = saved[0], saved[2]
            embed._STATS.update(saved[1])

    def test_api_treats_429_and_5xx_as_retryable(self):
        payload = b'{"data": [{"index": 0, "embedding": [0.0, 1.0]}]}'
        calls = {"n": 0}

        def fake_post(url, body, headers, timeout, size=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise embed.http_pool.HTTPStatusError(429, b'{"code":"Throttling"}')
            if calls["n"] == 2:
                raise embed.http_pool.HTTPStatusError(503, b"busy")
            return payload

        saved = (embed.API_KEY, embed._STATS.copy(), embed.http_pool.post)
        embed.API_KEY, embed.http_pool.post = "sk-test", fake_post
        try:
            self.assertEqual(embed._call_api_unlocked(["x"])[0], [0.0, 1.0])
            self.assertEqual(embed._STATS["api_429"], 1)
        finally:
            embed.API_KEY, embed.http_pool.post = saved[0], saved[2]
            embed._STATS.update(saved[1])

    def test_api_retries_upstream_internal_error_masked_as_400(self):
        """百炼实测会把后端抖动包成 400 + code=InternalError；这是瞬时故障，必须重试。"""
        payload = b'{"data": [{"index": 0, "embedding": [0.0, 1.0]}]}'
        body = (b'{"error":{"message":"Receive batching backend response failed!",'
                b'"type":"InternalError","code":"InternalError"}}')
        calls = {"n": 0}

        def fake_post(url, body_, headers, timeout, size=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise embed.http_pool.HTTPStatusError(400, body)
            return payload

        saved = (embed.API_KEY, embed._STATS.copy(), embed.http_pool.post)
        embed.API_KEY, embed.http_pool.post = "sk-test", fake_post
        try:
            self.assertEqual(embed._call_api_unlocked(["x"])[0], [0.0, 1.0])
        finally:
            embed.API_KEY, embed.http_pool.post = saved[0], saved[2]
            embed._STATS.update(saved[1])

        # 纯参数错误（400 + 无瞬时标记）仍然一次就失败，不浪费重试
        calls["n"] = 0

        def bad(url, body_, headers, timeout, size=None):
            calls["n"] += 1
            raise embed.http_pool.HTTPStatusError(400, b'{"code":"InvalidParameter"}')

        before = embed._STATS["api_give_up"]
        saved = (embed.API_KEY, embed._STATS.copy(), embed.http_pool.post)
        embed.API_KEY, embed.http_pool.post = "sk-test", bad
        try:
            with self.assertRaises(RuntimeError):
                embed._call_api_unlocked(["x"])
            self.assertEqual(calls["n"], 1)
            # 参数错误是「快速失败」，不算重试预算耗尽
            self.assertEqual(embed._STATS["api_give_up"], before)
        finally:
            embed.API_KEY, embed.http_pool.post = saved[0], saved[2]
            embed._STATS.update(saved[1])

    def test_retry_budget_exhaustion_is_counted(self):
        """重试预算耗尽 → 计数 + 抛错；Full 期间靠 /version 的 api_give_up 判断是配额还是 bug。"""
        def boom(url, body_, headers, timeout, size=None):
            raise TimeoutError("upstream timeout")

        saved = (embed.API_KEY, embed.RETRY_BUDGET, embed._STATS.copy(), embed.http_pool.post)
        before = embed._STATS["api_give_up"]
        embed.API_KEY, embed.RETRY_BUDGET, embed.http_pool.post = "sk-test", 0.01, boom
        try:
            with self.assertRaises(RuntimeError):
                embed._call_api_unlocked(["x"])
            self.assertEqual(embed._STATS["api_give_up"], before + 1)
        finally:
            (embed.API_KEY, embed.RETRY_BUDGET,
             embed.http_pool.post) = saved[0], saved[1], saved[3]
            embed._STATS.update(saved[2])

    def test_query_dates_parses_the_formats_we_actually_see(self):
        import retrieve
        self.assertEqual(retrieve.query_dates("Which image shown in 2024-06-17?")[0],
                         {(2024, 6, 17)})
        self.assertEqual(retrieve.query_dates("the conversation of July 21")[0], {(0, 7, 21)})
        self.assertIn((2024, 6, 17), retrieve.query_dates("June 17, 2024 and 2024/06/17")[0])
        self.assertEqual(retrieve.query_dates("2024年7月21日 有几张照片")[0], {(2024, 7, 21)})
        # 没有日期时不能凭空造出来（否则会误伤普通问题）
        self.assertEqual(retrieve.query_dates("How many photos are there?"), (set(), set()))
        self.assertEqual(retrieve.query_dates("version 1.2.3 notes"), (set(), set()))

    def test_date_bias_only_adds_never_subtracts(self):
        import retrieve
        docs = [{"created_at": "2024-06-17T09:00:00Z"},
                {"created_at": "2024-07-21T09:00:00Z"}]
        bias = retrieve.date_bias(docs, "Which image shown in 2024-06-17?", weight=0.1)
        self.assertEqual(bias, [0.1, 0.0])
        self.assertTrue(all(b >= 0 for b in bias))
        # 一条都不匹配 ⇒ 返回 None，完全不改动排序
        self.assertIsNone(retrieve.date_bias(docs, "on 2024-01-01", weight=0.1))

    def test_recency_bias_is_monotonic_and_cue_gated(self):
        import retrieve
        docs = [{"created_at": f"2024-06-{d:02d}T09:00:00Z"} for d in (1, 10, 20)]
        old_rule, old_w = retrieve.RECENCY_RULE, retrieve.RECENCY_WEIGHT
        try:
            retrieve.RECENCY_RULE, retrieve.RECENCY_WEIGHT = "update", 0.05
            bias = retrieve.recency_bias(docs, "what is his latest stance?")
            self.assertIsNotNone(bias)
            self.assertAlmostEqual(bias[0], -0.05, places=6)   # 最老
            self.assertAlmostEqual(bias[-1], 0.05, places=6)   # 最新
            self.assertEqual(bias, sorted(bias))
            # 没有更新线索 ⇒ 不启用（普通问题不该被时间戳带偏）
            self.assertIsNone(retrieve.recency_bias(docs, "what did he say about robots?"))
        finally:
            retrieve.RECENCY_RULE, retrieve.RECENCY_WEIGHT = old_rule, old_w

    def test_diversify_keeps_the_same_items_and_is_deterministic(self):
        import retrieve
        docs = [{"id": f"m{i}", "text": f"t{i}", "vec": [1.0, 0.0, 0.0] if i % 2 else [0.0, 1.0, 0.0]}
                for i in range(6)]
        ranked = [(i, 1.0 - i * 0.05) for i in range(6)]
        a = retrieve.diversify(ranked, docs, None, want=6, lam=0.2)
        b = retrieve.diversify(ranked, docs, None, want=6, lam=0.2)
        self.assertEqual([i for i, _ in a], [i for i, _ in b])       # 确定性
        self.assertEqual(sorted(i for i, _ in a), list(range(6)))     # 不丢也不增
        self.assertEqual(retrieve.diversify(ranked, docs, None, lam=0.0), ranked)

    def test_config_hash_covers_every_ranking_knob(self):
        """每个影响输出字节的开关改一下，config_hash 都必须变（之前漏过 DIVERSITY/DATE_WEIGHT）。

        每个变体起一个子进程读环境变量（配置在 import 时冻结）。
        """
        import subprocess
        knobs = {"FUSION": "rrf", "DENSE_WEIGHT": "0.9", "SPARSE_WEIGHT": "0.1", "RRF_K": "30",
                 "SPARSE_NORM": "max", "BM25_RARE_DF": "0.2", "TEMPORAL_RULE": "off",
                 "TEMPORAL_WEIGHT": "0.1", "DATE_WEIGHT": "0.1", "RECENCY_RULE": "update",
                 "RECENCY_WEIGHT": "0.1", "DEDUP_COS": "0.9", "DEDUP_POOL": "100",
                 "DIVERSITY": "0.1", "DIVERSITY_POOL": "20", "RERANK_MODE": "http",
                 "RERANK_MODEL": "x", "RERANK_TOP": "20", "RERANK_TOL": "0.001",
                 "EMBED_CONTEXT": "1", "EMBED_CONTEXT_CHARS": "100", "MAX_IMAGES": "3",
                 "MAX_RESULTS": "20"}
        code = "import server; print(server._config_fingerprint())"
        base_env = {k: v for k, v in os.environ.items() if k not in knobs}
        base_env.update(EMBED_MODE="hash", STARTUP_CHECK="0", DOTENV_DISABLE="1",
                        DB_PATH=os.path.join(tempfile.mkdtemp(), "h.db"))

        def fp(extra):
            out = subprocess.run([sys.executable, "-c", code], env={**base_env, **extra},
                                 cwd=os.path.join(ROOT, "service"), capture_output=True,
                                 text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout.strip().splitlines()[-1]

        base = fp({})
        self.assertEqual(base, fp({}), "指纹本身必须确定")
        unchanged = [k for k, v in knobs.items() if fp({k: v}) == base]
        self.assertEqual(unchanged, [], f"这些开关改了但 config_hash 没变：{unchanged}")

    def test_submission_docs_match_runtime_defaults(self):
        """SUBMISSION.md / README.md 写的数字必须等于代码默认值（审核按文档核对）。"""
        import subprocess
        out = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "doc_check.py")],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_rerank_default_model_is_the_supported_one(self):
        """默认精排模型必须是 qwen3-rerank：gte-rerank-v2 已标注下线，
        实测同一请求分值最大跳 1.3e-3、48 次里 4 次顺序翻转，不能作为默认。"""
        import retrieve
        if os.environ.get("RERANK_MODEL"):
            self.skipTest("环境显式指定了 RERANK_MODEL")
        self.assertEqual(retrieve.RERANK_MODEL, "qwen3-rerank")
        # 容差必须大于上游抖动，否则顺序仍会翻转
        self.assertGreaterEqual(retrieve.RERANK_TOL, 1e-5)

    def test_pack_roundtrip(self):
        v = [0.25, -0.5, 1.0]
        self.assertEqual(embed.unpack(embed.pack(v)), v)
        self.assertEqual(len(embed.pack(v)), len(v) * 4)
        self.assertEqual(struct.calcsize("<3f"), len(embed.pack(v)))


class HttpPoolTest(unittest.TestCase):
    """连接池必须真的复用连接：每次新建 TCP+TLS 是实测 p95 飙到秒级的元凶。"""

    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        cls.ports = []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                cls.ports.append(self.client_address[1])
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/x"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_sequential_posts_reuse_one_connection(self):
        import http_pool

        self.ports.clear()
        for _ in range(3):
            self.assertEqual(http_pool.post(self.url, b"{}", {}, 5.0), b'{"ok": true}')
        self.assertEqual(len(set(self.ports)), 1, f"应有 1 条连接，实际 {set(self.ports)}")

    def test_oversubscribed_pool_never_stalls(self):
        """池满时等待者不能额外占额度：曾经的实现会让 48 并发挂到 60 s 超时。"""
        import concurrent.futures

        import http_pool

        url = self.url
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
            futs = [ex.submit(http_pool.post, url, b"{}", {}, 10.0, 2) for _ in range(48)]
            done = [f.result(timeout=30) for f in futs]
        self.assertEqual(done, [b'{"ok": true}'] * 48)
        # 池上限是 2：最多 2 条并发连接
        self.assertLessEqual(len(set(self.ports[-48:])), 2 + 1)

    def test_error_status_surfaces_with_code_and_releases_slot(self):
        import http_pool

        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = b'{"code":"InvalidParameter"}'
                self.send_response(400)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_port}/bad"
        try:
            for _ in range(3):
                with self.assertRaises(http_pool.HTTPStatusError) as ctx:
                    http_pool.post(url, b"{}", {}, 5.0)
                self.assertEqual(ctx.exception.status, 400)
        finally:
            srv.shutdown()
            srv.server_close()


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = store.DB_PATH
        store.DB_PATH = os.path.join(self.tmp.name, "t.db")
        store.init()

    def tearDown(self):
        store.DB_PATH = self.old
        self.tmp.cleanup()

    def _item(self, rid, text, seq=None):
        return {"id": f"{rid}:{text}", "user_id": "u1", "session_id": "s", "request_id": rid,
                "seq": seq, "text": text, "images": [], "ts": None,
                "created_at": "2026-01-01T00:00:00Z", "vec": embed.pack(embed.embed([text])[0])}

    def test_global_monotonic_seq_across_adds(self):
        store.add_memories([self._item("r1", "first")])
        store.add_memories([self._item("r1", "second"), self._item("r2", "third")])
        docs = store.fetch_user("u1")
        self.assertEqual([d["text"] for d in docs], ["first", "second", "third"])
        self.assertEqual([d["seq"] for d in docs], [0, 1, 2])

    def test_user_cache_hits_and_appends_after_add(self):
        store.add_memories([self._item("r1", "first")])
        cold = {}
        first = store.fetch_user("u1", stats=cold)
        self.assertFalse(cold["cache_hit"])
        hot = {}
        hot_docs = store.fetch_user("u1", stats=hot)
        self.assertTrue(hot["cache_hit"])
        self.assertIsNot(hot_docs, first)
        hot_docs[0]["text"] = "caller mutation"
        self.assertEqual(store.fetch_user("u1")[0]["text"], "first")

        store.add_memories([self._item("r2", "second")])
        refreshed = {}
        docs = store.fetch_user("u1", stats=refreshed)
        self.assertTrue(refreshed["cache_hit"])  # append-only Add keeps the cache warm
        self.assertEqual([d["text"] for d in docs], ["first", "second"])
        self.assertEqual([d["seq"] for d in docs], [0, 1])
        self.assertEqual(store.count("u1"), 2)

        # A replayed Add writes nothing and must not duplicate cached rows.
        store.add_memories([self._item("r2", "second")])
        self.assertEqual([d["text"] for d in store.fetch_user("u1")], ["first", "second"])

        # Appended cache rows must match what a fresh DB read would return.
        cached_rows = [(d["text"], d["created_at"], d["seq"]) for d in store.fetch_user("u1")]
        db_rows = [(d["text"], d["created_at"], d["seq"])
                   for d in store.fetch_user("u1", with_images=True)]
        self.assertEqual(cached_rows, db_rows)

    def test_user_cache_accounting_matches_entries(self):
        store.add_memories([self._item("r1", "first")])
        store.fetch_user("u1")
        store.add_memories([self._item("r2", "second"), self._item("r3", "third")])
        store.fetch_user("u1")
        self.assertEqual(store._USER_CACHE_BYTES,
                         sum(size for _, size in store._USER_CACHE.values()))
        self.assertEqual(len(store.fetch_user("u1")), 3)

    def test_user_cache_oversized_add_drops_entry_instead_of_corrupting(self):
        store.add_memories([self._item("r1", "first")])
        store.fetch_user("u1")
        saved = store._USER_CACHE_MAX_BYTES
        try:
            store._USER_CACHE_MAX_BYTES = 1024  # below the existing entry size
            store.add_memories([self._item("r2", "second")])
            self.assertNotIn((store.DB_PATH, "u1"), store._USER_CACHE)
            self.assertEqual([d["text"] for d in store.fetch_user("u1")], ["first", "second"])
            self.assertNotIn((store.DB_PATH, "u1"), store._USER_CACHE)
        finally:
            store._USER_CACHE_MAX_BYTES = saved
            store._USER_CACHE.clear()
            store._USER_CACHE_BYTES = 0

    def test_isolation(self):
        store.add_memories([self._item("r1", "a")])
        self.assertEqual(store.fetch_user("u2"), [])
        self.assertEqual(store.count("u1"), 1)
        self.assertEqual(store.count("u2"), 0)

    def test_retry_after_embedding_failure_writes_exactly_once(self):
        """平台会用同一 request_id 重试最多 32 次：中途失败不能留下半截或重复记忆。"""
        import server

        saved_batch = embed.BATCH
        embed.BATCH = 5                     # 25 条 → 5 个 batch，便于在第 3 个 batch 掉线
        real = embed._hash_embed
        state = {"n": 0}

        def flaky(text):
            state["n"] += 1
            if state["n"] > 10:             # 前 2 个 batch 正常，之后上游 429
                raise RuntimeError("simulated 429")
            return real(text)

        req = {"request_id": "r-partial", "user_id": "u-partial", "session_id": "s1",
               "messages": [{"role": "user", "content": f"独立记忆内容 alpha{i}",
                             "timestamp": 1735689600000 + i} for i in range(25)]}
        embed._hash_embed = flaky
        try:
            with self.assertRaises(Exception):
                server.do_add(req)
            self.assertEqual(len(store.fetch_user("u-partial")), 0, "失败不得留下部分写入")
            embed._hash_embed = real       # 上游恢复，平台重试
            server.do_add(req)
            server.do_add(req)             # 再重试一次：幂等，不重复
            # 另一个崩溃窗口：记忆已落库、幂等记录还没写就崩了 → 重试仍不能重复
            with store._conn() as c:
                c.execute("DELETE FROM idem WHERE user_id=? AND request_id=?",
                          ("u-partial", "r-partial"))
            server.do_add(req)
        finally:
            embed._hash_embed = real
            embed.BATCH = saved_batch
        self.assertEqual(len(store.fetch_user("u-partial")), 25)

    def test_idempotent_response(self):
        self.assertIsNone(store.get_idempotent("u1", "r1"))
        store.put_idempotent("u1", "r1", {"success": True, "request_id": "r1"})
        self.assertEqual(store.get_idempotent("u1", "r1")["request_id"], "r1")
        self.assertIsNone(store.get_idempotent("u2", "r1"))

    def test_ping(self):
        self.assertTrue(store.ping())


class TemporalTest(unittest.TestCase):
    """时态一致性规则：近分平局时优先回答「现在」问的旧事实不能被抢到第一。

    阈值取自留出评测（`tools/tune.py --temporal`）：hand 留出 temporal MRR 0.750→1.000，
    合成留出 0.909→1.000，且没有 query 类型退化。
    """

    def _docs(self, texts):
        vecs = embed.embed(texts)
        return [
            {"id": f"d{i}", "text": t, "images": [], "seq": i,
             "created_at": f"2026-01-01T00:00:{i:02d}Z", "ts": None, "vec": embed.pack(v)}
            for i, (t, v) in enumerate(zip(texts, vecs))
        ]

    def test_query_tense_detection(self):
        self.assertEqual(retrieve.query_tense("Alice 现在住在哪个城市？"), "present")
        self.assertEqual(retrieve.query_tense("Where does Bob live now?"), "present")
        self.assertEqual(retrieve.query_tense("Alice 以前住在哪里？"), "past")
        self.assertEqual(retrieve.query_tense("Alice 的护照号是多少？"), None)
        # 同时含两个时态：不确定，不启用
        self.assertIsNone(retrieve.query_tense("以前住哪，现在住哪？"))

    def test_english_cues_need_word_boundaries(self):
        # 子串误触发：know/snowing ⊃ now、performer ⊃ former
        for q in ("Do you know what species is shown?", "Is it snowing?",
                  "Which performer sang?", "Did she acknowledge it?"):
            self.assertIsNone(retrieve.query_tense(q), q)
        self.assertEqual(retrieve.doc_tense_state({"text": "The performer was great."}), "neutral")
        self.assertEqual(retrieve.doc_tense_state({"text": "A former colleague."}), "past")
        self.assertEqual(retrieve.query_tense("Where did Alice used to live?"), "past")

    def test_present_query_demotes_past_only_memory(self):
        docs = self._docs(["Alice 去年住在慕尼黑。", "Alice 现在住在柏林。"])
        qv = embed.embed(["Alice 现在住在哪个城市？"])[0]
        ranked = retrieve.run(qv, "Alice 现在住在哪个城市？", docs)
        self.assertEqual(ranked[0][0], 1, "问「现在」时旧事实不应排第一")

    def test_mixed_tense_memory_is_not_penalised(self):
        mixed = "Alice 去年住在慕尼黑，现在住在柏林。"
        self.assertEqual(retrieve.doc_tense_state({"text": mixed}), "neutral")
        self.assertEqual(retrieve.tense_bias([{"text": mixed}], "present"), None)

    def test_past_query_promotes_past_memory(self):
        docs = self._docs(["Alice 现在住在柏林。", "Alice 去年住在慕尼黑。"])
        q = "Alice 去年住在哪个城市？"
        qv = embed.embed([q])[0]
        ranked = retrieve.run(qv, q, docs)
        self.assertEqual(ranked[0][0], 1)

    def test_rule_is_inert_without_tense_cue(self):
        docs = self._docs(["Alice 去年住在慕尼黑。", "Alice 现在住在柏林。"])
        q = "Alice 的护照号是多少？"
        self.assertIsNone(retrieve.tense_bias(docs, retrieve.query_tense(q)))
        qv = embed.embed([q])[0]
        self.assertEqual(retrieve.run(qv, q, docs), retrieve.hybrid(qv, q, docs))

    def test_bias_keeps_scores_bounded_and_deterministic(self):
        docs = self._docs(["Alice 去年住在慕尼黑。", "Alice 现在住在柏林。", "Bob likes jazz."])
        q = "Alice 现在住在哪个城市？"
        qv = embed.embed([q])[0]
        first = retrieve.run(qv, q, docs)
        self.assertEqual(first, retrieve.run(qv, q, docs))
        for _, score in first:
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)

    def test_run_applies_same_temporal_bias_as_hybrid(self):
        """线上 Search 走 run()：时态规则必须在 run() 生效，且分数与 hybrid() 一致。"""
        docs = self._docs(["Alice 去年住在慕尼黑。", "Alice 住在柏林。"] +
                          [f"无关记录 {i}" for i in range(5)])
        q = "Alice 现在住在哪个城市？"
        qv = embed.embed([q])[0]
        saved = retrieve.TEMPORAL_RULE
        try:
            retrieve.TEMPORAL_RULE = "off"
            off = dict(retrieve.run(qv, q, docs))
            retrieve.TEMPORAL_RULE = "tense"
            on = dict(retrieve.run(qv, q, docs))
            self.assertEqual(on, dict(retrieve.hybrid(qv, q, docs)))
        finally:
            retrieve.TEMPORAL_RULE = saved
        self.assertAlmostEqual(off[0] - on[0], retrieve.TEMPORAL_WEIGHT, places=9)
        self.assertEqual(off[1], on[1])

    def test_rule_can_be_disabled(self):
        docs = self._docs(["Alice 去年住在慕尼黑。", "Alice 现在住在柏林。"])
        q = "Alice 现在住在哪个城市？"
        qv = embed.embed([q])[0]
        saved = retrieve.TEMPORAL_RULE
        try:
            retrieve.TEMPORAL_RULE = "off"
            self.assertEqual(retrieve.run(qv, q, docs), retrieve.hybrid(qv, q, docs))
        finally:
            retrieve.TEMPORAL_RULE = saved


class ImageDedupTest(unittest.TestCase):
    """图集式检索：正文相同（甚至都是占位符）但图片不同的记忆不能被当成重复删掉。"""

    def test_distinct_images_survive_dedup(self):
        texts = ["(image only)"] * 3 + ["(image only)"]
        vecs = embed.embed(texts)
        docs = [{"id": f"i{k}", "text": t, "images": [], "seq": k, "ts": None,
                 "created_at": f"2026-01-01T00:00:{k:02d}Z", "vec": embed.pack(v),
                 "img_sig": f"sig{k}" if k < 3 else "sig0"}
                for k, (t, v) in enumerate(zip(texts, vecs))]
        q = embed.embed(["photo"])[0]
        got = [docs[i]["id"] for i, _ in retrieve.run(q, "photo", docs)]
        # i3 与 i0 同图同文 ⇒ 真重复，只留一条；平局按 created_at 降序，留最新的 i3
        self.assertEqual(sorted(got), ["i1", "i2", "i3"])

    def test_text_duplicates_without_images_still_dedup(self):
        docs = [{"id": f"t{k}", "text": "Alice lives in Berlin.", "images": [], "seq": k,
                 "ts": None, "created_at": "2026-01-01T00:00:00Z",
                 "vec": embed.pack(embed.embed(["Alice lives in Berlin."])[0])} for k in range(3)]
        q = embed.embed(["Alice"])[0]
        self.assertEqual(len(retrieve.run(q, "Alice", docs)), 1)


class CaptionTest(unittest.TestCase):
    """可选的 gpt-4o-mini 图片描述：只追加、可缓存、确定性、合规闸门。"""

    @classmethod
    def setUpClass(cls):
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import caption

        cls.caption = caption
        cls.calls = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = _json.loads(self.rfile.read(n).decode())
                cls.calls.append(req)
                if self.server.fail:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"bad")
                    return
                url = req["messages"][0]["content"][1]["image_url"]["url"]
                body = _json.dumps({"choices": [{"message": {
                    "content": f"A green bar chart  ({len(url)})"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.srv.fail = False
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        c = self.caption
        self._saved = (c.MODE, c.API_KEY, c.BASE, c.MODEL, c.STRICT, store.DB_PATH)
        c.MODE, c.API_KEY, c.MODEL, c.STRICT = "openai", "k", "gpt-4o-mini", True
        c.BASE = f"http://127.0.0.1:{self.srv.server_port}"
        store.DB_PATH = os.path.join(self.tmp.name, f"cap-{self._testMethodName}.db")
        c._MEM.clear()
        self.srv.fail = False
        self.calls.clear()

    def tearDown(self):
        c = self.caption
        c.MODE, c.API_KEY, c.BASE, c.MODEL, c.STRICT, store.DB_PATH = self._saved
        c._MEM.clear()

    def test_describe_is_deterministic_and_cached(self):
        img = "data:image/png;base64,AAAA"
        first = self.caption.describe([img])
        self.caption._MEM.clear()  # 进程重启后仍命中 SQLite 持久缓存
        second = self.caption.describe([img])
        self.assertEqual(first, ["A green bar chart (26)"])
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        req = self.calls[0]
        self.assertEqual(req["model"], "gpt-4o-mini")
        self.assertEqual(req["temperature"], 0)

    def test_non_compliant_model_is_rejected(self):
        self.caption.MODEL = "gpt-4o"
        ok, msg = self.caption.check()
        self.assertFalse(ok)
        self.assertIn("gpt-4o-mini", msg)

    def test_strict_failure_raises_and_lenient_skips(self):
        self.srv.fail = True
        with self.assertRaises(self.caption.CaptionError):
            self.caption.describe(["data:image/png;base64,BBBB"])
        self.caption.STRICT = False
        self.assertEqual(self.caption.describe(["data:image/png;base64,BBBB"]), [""])

    def test_disabled_makes_no_calls(self):
        self.caption.MODE = "none"
        self.assertEqual(self.caption.describe(["data:image/png;base64,CCCC"]), [""])
        self.assertEqual(self.calls, [])

    def test_image_sig_orders_and_distinguishes(self):
        a, b = "data:image/png;base64,AAAA", "data:image/png;base64,BBBB"
        sig = self.caption.image_sig
        self.assertEqual(sig([]), "")
        self.assertEqual(sig([a, b]), sig([a, b]))
        self.assertNotEqual(sig([a, b]), sig([b, a]))
        self.assertNotEqual(sig([a]), sig([b]))


class RerankTest(unittest.TestCase):
    """精排旁路：保序重排 / 分数量纲 / 降级与熔断。

    精排是唯一不受官方模型限制的增益点（本地 bge-reranker-v2-m3），
    但它同时也是唯一的外部依赖 ⇒ 必须证明「挂了也不影响 Search」。
    """

    @classmethod
    def setUpClass(cls):
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        cls.plan = {"rows": [], "mode": "full"}  # mode: full | partial | error

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = _json.loads(self.rfile.read(n).decode() or "{}")
                docs = req.get("documents") or []
                mode = self.server.plan["mode"]
                if mode == "error":
                    body = b'{"detail":"boom"}'
                    self.send_response(500)
                elif mode == "flaky" and not self.server.plan.get("flaked"):
                    # 模拟「响应读到一半连接被掐断」：不发 Content-Length、直接关连接
                    self.server.plan["flaked"] = True
                    self.wfile.write(b'{"results":[{"inde')
                    self.wfile.flush()
                    self.close_connection = True
                    return
                else:
                    rows = self.server.plan["rows"]
                    if mode == "partial":
                        rows = rows[: max(1, len(docs) // 2)]
                    body = _json.dumps({"results": rows}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.srv.plan = cls.plan
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/rerank"
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self._saved = (retrieve.RERANK_MODE, retrieve.RERANK_URL, retrieve.RERANK_TOP,
                       retrieve.RERANK_FAILS, retrieve.RERANK_COOLDOWN, retrieve.DEDUP_COS)
        retrieve.DEDUP_COS = 1.1  # 本组只测精排：关掉近重复去重，避免合成语料被合并
        retrieve._rerank_fails = 0
        retrieve._rerank_cooldown_until = 0.0
        retrieve._RERANK_STATS.update({"calls": 0, "fails": 0, "degraded": 0,
                                       "cooldown_skips": 0})
        retrieve.RERANK_MODE = "http"
        retrieve.RERANK_URL = self.url
        retrieve.RERANK_TOP = 4
        self.plan["mode"] = "full"
        self.plan["rows"] = []

    def tearDown(self):
        (retrieve.RERANK_MODE, retrieve.RERANK_URL, retrieve.RERANK_TOP,
         retrieve.RERANK_FAILS, retrieve.RERANK_COOLDOWN, retrieve.DEDUP_COS) = self._saved
        retrieve._rerank_fails = 0
        retrieve._rerank_cooldown_until = 0.0

    def _docs(self, n=6):
        texts = [f"第{i}号独立事实：编号 {1000 + i}，地点 {chr(65 + i)} 市。" for i in range(n)]
        vecs = [[1.0 if j == 0 else 0.01 * (i + 1) for j in range(3)] for i in range(n)]
        return [
            {"id": f"d{i}", "text": t, "images": [], "seq": i,
             "created_at": f"2026-01-01T00:00:{i:02d}Z", "vec": embed.pack(v)}
            for i, (t, v) in enumerate(zip(texts, vecs))
        ]

    def test_rerank_reorders_and_keeps_score_band(self):
        """精排序生效；头部分数落在融合区间内、尾部不反超头部。"""
        docs = self._docs()
        qv = embed.embed(["fact"])[0]
        fused = retrieve.hybrid(qv, "fact", docs)
        fused_scores = {docs[i]["id"]: s for i, s in fused}
        # TEI 契约里的 index 是「提给精排的候选列表」里的位置，不是文档下标
        head_ids = [docs[i]["id"] for i, _ in fused[:retrieve.RERANK_TOP]]
        winner = head_ids[1]
        self.plan["rows"] = [
            {"index": pos, "score": 0.9 if did == winner else 0.5 - 0.1 * pos}
            for pos, did in enumerate(head_ids)
        ]
        got = retrieve.run(qv, "fact", docs)
        self.assertEqual(docs[got[0][0]]["id"], winner, "精排第一名必须置顶")
        scores = [s for _, s in got]
        self.assertEqual(scores, sorted(scores, reverse=True), "输出必须按 score 降序")
        self.assertTrue(all(0.0 <= s <= 1.0 for s in scores), scores)
        # 头部仍然 ≥ 尾部：不能出现尾部融合分反超头部精排分
        head_min = min(s for _, s in got[:4])
        tail_max = max(s for _, s in got[4:]) if len(got) > 4 else -1.0
        self.assertGreaterEqual(head_min, tail_max)
        # 量纲守恒：头部最高分不超过融合分最高分
        self.assertLessEqual(max(scores), max(fused_scores.values()) + 1e-12)

    def test_rerank_error_degrades_and_breaks_circuit(self):
        """服务 500：首次降级为融合序；连续失败达阈值后进入冷却，不再逐次等超时。"""
        import time

        docs = self._docs()
        qv = embed.embed(["fact"])[0]
        fused = [(docs[i]["id"], round(s, 12)) for i, s in retrieve.hybrid(qv, "fact", docs)]
        self.plan["mode"] = "error"
        retrieve.RERANK_FAILS, retrieve.RERANK_COOLDOWN = 2, 30
        self.assertEqual([(docs[i]["id"], round(s, 12)) for i, s in retrieve.run(qv, "fact", docs)], fused)
        self.assertEqual(retrieve._rerank_cooldown_until, 0.0, "一次失败不该熔断")
        retrieve.run(qv, "fact", docs)
        self.assertGreater(retrieve._rerank_cooldown_until, time.time(), "达到阈值应进入冷却")

    def test_rerank_jitter_does_not_change_scores_or_order(self):
        """上游精排分双峰抖动 1.4e-4 且会写进响应：容差以内必须按并列处理。"""
        docs = self._docs()
        qv = embed.embed(["fact"])[0]
        ranked = retrieve.hybrid(qv, "fact", docs)
        head_ids = [docs[i]["id"] for i, _ in ranked[:retrieve.RERANK_TOP]]
        base = [{"index": pos, "score": 0.7 - 0.05 * pos} for pos, _ in enumerate(head_ids)]
        # 两套精确值：实测 gte-rerank-v2 在 0.104684 / 0.104543 之间跳（差 1.4e-4），
        # 而且靠近并列的两条会跨越容差——第二套特意把相邻两条压到 5e-5 以内。
        variant_a = [{"index": row["index"], "score": row["score"] + 7e-5 * (i % 2)}
                     for i, row in enumerate(base)]
        variant_b = [{"index": row["index"], "score": row["score"] - 7e-5 * (i % 2)}
                     for i, row in enumerate(base)]
        outs = []
        for rows in (base, variant_a, variant_b):
            self.plan["rows"] = rows
            self.plan["mode"] = "full"
            outs.append(retrieve.rerank("q", ranked, docs))
        for o in outs[1:]:
            self.assertEqual([(s) for _, s in o], [s for _, s in outs[0]],
                             "上游抖动不应改变响应里的分数")
            self.assertEqual([docs[i]["id"] for i, _ in o],
                             [docs[i]["id"] for i, _ in outs[0]],
                             "上游抖动不应改变排序")

    def test_rerank_retries_transient_break_before_degrading(self):
        """实测：压测后精排偶发断连，若直接降级，同一 query 重放的排序就变了。
        一次断连必须靠重试吃掉，不能落到降级路径。"""
        docs = self._docs()
        qv = embed.embed(["fact"])[0]
        head_ids = [docs[i]["id"] for i, _ in retrieve.hybrid(qv, "fact", docs)[:retrieve.RERANK_TOP]]
        self.plan["rows"] = [{"index": pos, "score": 0.9 - 0.1 * pos}
                             for pos, _ in enumerate(head_ids)]
        self.plan["mode"] = "flaky"
        self.plan["flaked"] = False
        got = retrieve.run(qv, "fact", docs)
        self.assertEqual(retrieve._RERANK_STATS["fails"], 0, "断连应被重试吃掉")
        self.assertEqual(docs[got[0][0]]["id"], head_ids[0], "重试成功后必须用精排结果")
        self.assertEqual(retrieve._rerank_cooldown_until, 0.0)

    def test_rerank_partial_response_is_rejected(self):
        """返回条数不足 ⇒ 视为无效响应，整体降级（不能丢候选）。"""
        docs = self._docs()
        qv = embed.embed(["fact"])[0]
        fused = [(docs[i]["id"], round(s, 12)) for i, s in retrieve.hybrid(qv, "fact", docs)]
        self.plan["mode"] = "partial"
        self.plan["rows"] = [{"index": i, "score": 0.5} for i in range(len(docs))]
        got = retrieve.run(qv, "fact", docs)
        self.assertEqual(len(got), len(fused), "候选不能少")
        self.assertEqual([(docs[i]["id"], round(s, 12)) for i, s in got], fused)

    def test_equal_rerank_scores_preserve_fusion_order_and_scores(self):
        docs = self._docs()
        ranked = [(i, 0.9 - i * 0.1) for i in range(len(docs))]
        for position, (i, _) in enumerate(ranked):
            docs[i]["created_at"] = f"{len(ranked) - position:02d}"
        self.plan["rows"] = [{"index": i, "score": 0.5} for i in range(4)]
        self.assertEqual(retrieve.rerank("query", ranked, docs), ranked)

    def test_duplicate_rerank_indices_degrade_without_losing_candidates(self):
        docs = self._docs()
        ranked = [(i, 0.9 - i * 0.1) for i in range(len(docs))]
        self.plan["rows"] = [
            {"index": 0, "score": 0.9}, {"index": 1, "score": 0.8},
            {"index": 1, "score": 0.7}, {"index": 3, "score": 0.6},
        ]
        self.assertEqual(retrieve.rerank("query", ranked, docs), ranked)

    def test_non_finite_rerank_score_degrades(self):
        docs = self._docs()
        ranked = [(i, 0.9 - i * 0.1) for i in range(len(docs))]
        self.plan["rows"] = [
            {"index": 0, "score": 0.9}, {"index": 1, "score": float("nan")},
            {"index": 2, "score": 0.7}, {"index": 3, "score": 0.6},
        ]
        self.assertEqual(retrieve.rerank("query", ranked, docs), ranked)


class FusionRegressionTest(unittest.TestCase):
    """回归测试：修掉的两个真实缺陷 + 融合契约。

    缺陷 1：BM25 全零时 _rank() 退化成按文档插入顺序打分，最早写入的分块白拿名次分
    缺陷 2：RRF 只取名次，把 cos 0.64 vs 0.27 压成几乎并列，弱 sparse 也能掀翻强 dense
    缺陷 3：中文只切 bigram，"下周几开会" 与 "会议安排在周五" 零重叠
    缺陷 4：score 上限恒为 2/(K+1)≈0.0328，无法当阈值用
    """

    def _docs(self, texts, vecs=None):
        if vecs is None:
            vecs = embed.embed(texts)
        return [
            {"id": f"d{i}", "text": t, "images": [], "seq": i,
             "created_at": f"2026-01-01T00:00:{i:02d}Z", "vec": embed.pack(v)}
            for i, (t, v) in enumerate(zip(texts, vecs))
        ]

    def test_sparse_all_zero_does_not_reward_insertion_order(self):
        """三篇互不相关：dense 明确的赢家不能因为排在后面就输给 d0。"""
        texts = ["alpha alpha", "beta beta", "gamma gamma"]
        docs = self._docs(texts)
        qv = embed.embed(["gamma"])[0]
        ranked = retrieve.hybrid(qv, "gamma", docs)
        self.assertEqual(ranked[0][0], 2, "dense 赢家必须是第 3 篇，不是插入最早的 d0")
        # 稀疏侧真的全零（无词面重叠）
        self.assertIsNone(retrieve.bm25_norm(retrieve.bm25_scores("zzzz", docs)))

    def test_rrf_mode_also_abstains_on_zero_sparse(self):
        saved = retrieve.FUSION
        try:
            retrieve.FUSION = "rrf"
            docs = self._docs(["alpha alpha", "beta beta", "gamma gamma"])
            qv = embed.embed(["gamma"])[0]
            self.assertEqual(retrieve.hybrid(qv, "zzzz", docs)[0][0], 2)
        finally:
            retrieve.FUSION = saved

    def test_dense_winner_not_flipped_by_weak_sparse(self):
        """cos 0.71 的文档不能被「只沾一个通用词」的另一篇挤下去。"""
        dim = 8
        q = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        strong = [0.71, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]   # cos=0.71
        weak = [0.31, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]     # cos=0.31
        docs = self._docs(["柏林的公寓租金是九八零欧", "柏林的天气不错"], [strong, weak])
        ranked = retrieve.hybrid(q, "柏林的公寓租金是九八零欧", docs)
        self.assertEqual(ranked[0][0], 0)

    def test_cjk_unigram_recall_keeps_bm25_nonzero(self):
        """'下周几开会' 与 '会议安排在周五' 只共享单字；bigram-only 会全零。"""
        docs = self._docs(["会议安排在周五 14:00", "天气不错"])
        sparse = retrieve.bm25_scores("下周几开会", docs)
        self.assertGreater(sparse[0], 0.0, "单字回退应让 BM25 非零")
        self.assertEqual(sparse[1], 0.0)

    def test_score_is_bounded_and_thresholdable(self):
        """score ∈ [0,1]：相关文档高、无关文档低，可直接做阈值。"""
        docs = self._docs(["Alice lives in Berlin.", "Bob plays jazz.",
                           "weather is nice today", "cats sleep a lot"])
        qv = embed.embed(["Where does Alice live?"])[0]
        ranked = retrieve.hybrid(qv, "Where does Alice live?", docs)
        scores = [s for _, s in ranked]
        self.assertTrue(all(0.0 <= s <= 1.0 for s in scores), scores)
        self.assertAlmostEqual(ranked[0][1], max(scores), places=12)
        self.assertGreater(ranked[0][1], ranked[-1][1])

    def test_full_order_is_deterministic_and_tie_broken(self):
        """并列必须按 created_at 降序 → id 升序 全序，不能受 dict 顺序影响。

        平台 Answer 侧遇到冲突时偏最新记忆，所以平局把后写入的排前
        （同一事实被重述时，先看到的是最新那次确认）。
        """
        v = embed.embed(["same text"])[0]
        a = self._docs(["same text", "same text"], [v, v])
        b = list(reversed(a))
        qv = embed.embed(["same text"])[0]
        ra = retrieve.hybrid(qv, "same text", a)
        rb = retrieve.hybrid(qv, "same text", b)
        self.assertEqual([a[i]["id"] for i, _ in ra][:2], ["d1", "d0"])
        self.assertEqual([s for _, s in ra], [s for _, s in rb])
        # 同一时间戳时退化到 id 升序
        same = [dict(d, created_at="2026-01-01T00:00:00Z") for d in a]
        rc = retrieve.hybrid(qv, "same text", same)
        self.assertEqual([same[i]["id"] for i, _ in rc][:2], ["d0", "d1"])

    def test_rare_df_filter_ignores_common_terms_only(self):
        """稀有词过滤：只剩通用词时应回退，不把 BM25 打成全零。"""
        docs = self._docs(["用户住在柏林", "用户住在慕尼黑", "用户住在汉堡"])
        saved = retrieve.BM25_RARE_DF
        try:
            retrieve.BM25_RARE_DF = 0.15
            self.assertIsNotNone(retrieve.bm25_norm(retrieve.bm25_scores("住在哪", docs)))
            retrieve.BM25_RARE_DF = 1.5      # 放宽 ⇒ 与不过滤一致
            loose = retrieve.bm25_scores("用户住在柏林", docs)
            retrieve.BM25_RARE_DF = 0.0
            none_f = retrieve.bm25_scores("用户住在柏林", docs)
            self.assertEqual([round(x, 9) for x in loose], [round(x, 9) for x in none_f])
        finally:
            retrieve.BM25_RARE_DF = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
