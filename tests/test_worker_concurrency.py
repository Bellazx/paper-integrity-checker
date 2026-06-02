"""Concurrency tests for the dev/test review API execution layers.

Verifies api/worker.py._run_pipeline_inner and api/routes/run.py._concurrent_stream
actually run detection/review concurrently (capped), collect all results, classify
high-risk correctly, and advance progress to total.
"""
import asyncio
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.worker as worker
import api.routes.run as run_route
from api.models import PaperInfo


class _FakeTM:
    """Minimal TaskManager stand-in capturing update_task calls."""
    def __init__(self):
        self.progress = []
        self.status = []
        self.final = None

    def update_task(self, task_id, **kw):
        if "progress" in kw:
            self.progress.append(kw["progress"])
        if "status" in kw:
            self.status.append(kw["status"])
        if kw.get("status") and str(kw["status"]).endswith("COMPLETED"):
            self.final = kw.get("result")


def _papers(n):
    return [PaperInfo(doi_slug=f"10.1_p{i}", doi=f"10.1/p{i}",
                      input_dir=f"/x/p{i}", output_dir=f"/y/p{i}") for i in range(n)]


class WorkerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_detection_and_review_run_concurrently(self):
        papers = _papers(6)
        SLEEP = 0.3

        async def fake_detect(**kw):
            await asyncio.sleep(SLEEP)
            # odd-indexed papers are high risk
            idx = int(kw["doi"].split("p")[-1])
            return {"summary": {}, "paper": {"doi": kw["doi"]},
                    "_risk": "高风险" if idx % 2 else "低风险"}

        def fake_overall(findings):
            return {"level": findings.get("_risk", "低风险")}

        async def fake_review(**kw):
            await asyncio.sleep(SLEEP)
            return {"doi": kw["doi"], "result": "低风险", "verdict": "建议低风险",
                    "image_review": "x", "data_review": "x", "reason": "x"}

        async def fake_gen_reports(results, d, t):
            return ["r.pdf"]

        tm = _FakeTM()
        with mock.patch.object(worker, "run_detection_single", fake_detect), \
             mock.patch.object(worker, "run_review_single", fake_review), \
             mock.patch.object(worker, "generate_reports", fake_gen_reports), \
             mock.patch("modules.chinese_report_generator._compute_overall_risk", fake_overall), \
             mock.patch.object(worker.Path, "exists", lambda self: True):
            t0 = time.monotonic()
            await worker._run_pipeline_inner("task1", tm, papers,
                                             {"max_workers": 4, "table_name": "yujing_quanliang"})
            elapsed = time.monotonic() - t0

        # 6 papers, detect capped at min(4,2)=2 -> 3 waves ~0.9s; review 3 high-risk at 4 -> 1 wave ~0.3s.
        # Serial would be 6*0.3 + 3*0.3 = 2.7s. Concurrent should be well under 2.0s.
        self.assertLess(elapsed, 2.0, f"not concurrent enough: {elapsed:.2f}s")
        # 3 odd-indexed papers are high risk -> 3 reviews
        self.assertEqual(tm.final["high_risk_detected"], 3)
        self.assertEqual(tm.final["reviewed"], 3)
        # detection progress reached total
        det = [p for p in tm.progress if p.get("stage") == "detection"]
        self.assertEqual(max(p["current"] for p in det), 6)

    async def test_review_error_is_captured_concurrently(self):
        papers = _papers(2)

        async def fake_detect(**kw):
            return {"summary": {}, "paper": {"doi": kw["doi"]}}

        def fake_overall(findings):
            return {"level": "高风险"}  # both high risk

        async def fake_review(**kw):
            if kw["doi"].endswith("p0"):
                raise RuntimeError("boom")
            return {"doi": kw["doi"], "result": "低风险", "verdict": "建议低风险",
                    "image_review": "x", "data_review": "x", "reason": "x"}

        async def fake_gen_reports(results, d, t):
            return []

        tm = _FakeTM()
        with mock.patch.object(worker, "run_detection_single", fake_detect), \
             mock.patch.object(worker, "run_review_single", fake_review), \
             mock.patch.object(worker, "generate_reports", fake_gen_reports), \
             mock.patch("modules.chinese_report_generator._compute_overall_risk", fake_overall), \
             mock.patch.object(worker.Path, "exists", lambda self: True):
            await worker._run_pipeline_inner("task2", tm, papers,
                                             {"max_workers": 4})
        # both reviewed (one ok, one error-captured as 高风险)
        self.assertEqual(tm.final["reviewed"], 2)
        self.assertEqual(tm.final["confirmed_high"], 1)  # the errored one


class ConcurrentStreamHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_stream_caps_and_yields_all(self):
        items = list(range(6))
        active = 0
        peak = 0

        async def worker_fn(item, emit):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            emit(f"start{item}\n")
            await asyncio.sleep(0.2)
            emit(f"done{item}\n")
            active -= 1

        events = []
        async for ev in run_route._concurrent_stream(items, worker_fn, limit=2):
            events.append(ev)

        self.assertLessEqual(peak, 2, f"exceeded concurrency cap: peak={peak}")
        # every item produced 2 events
        self.assertEqual(len(events), 12)
        for i in range(6):
            self.assertIn(f"done{i}\n", events)


if __name__ == "__main__":
    unittest.main()
