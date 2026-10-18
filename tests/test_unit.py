"""纯单元测试：预算裁剪 / 混合检索 / 存储 / embedding（hash 模式）。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
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

    def test_pack_roundtrip(self):
        v = [0.25, -0.5, 1.0]
        self.assertEqual(embed.unpack(embed.pack(v)), v)
        self.assertEqual(len(embed.pack(v)), len(v) * 4)
        self.assertEqual(struct.calcsize("<3f"), len(embed.pack(v)))


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

    def test_isolation(self):
        store.add_memories([self._item("r1", "a")])
        self.assertEqual(store.fetch_user("u2"), [])
        self.assertEqual(store.count("u1"), 1)
        self.assertEqual(store.count("u2"), 0)

    def test_idempotent_response(self):
        self.assertIsNone(store.get_idempotent("u1", "r1"))
        store.put_idempotent("u1", "r1", {"success": True, "request_id": "r1"})
        self.assertEqual(store.get_idempotent("u1", "r1")["request_id"], "r1")
        self.assertIsNone(store.get_idempotent("u2", "r1"))

    def test_ping(self):
        self.assertTrue(store.ping())


if __name__ == "__main__":
    unittest.main(verbosity=2)
