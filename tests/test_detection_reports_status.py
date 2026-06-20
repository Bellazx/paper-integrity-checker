"""detection_reports status lifecycle + (submission_no, fold_name) keying.

Covers the two-phase 初审(status=0) → 复审(status=2) flow and the NULL-fold handling
for single-paper submissions. The DB is replaced with a fake connection that records
executed SQL + params, so no SQL Server is touched.

Run:  python3 -m unittest tests.test_detection_reports_status
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.services.detection_reports_db as drdb
from api.models import PaperInfo, TaskStatus


class FakeCursor:
    def __init__(self, fetch_result=None):
        self.statements = []          # list of (sql, params)
        if isinstance(fetch_result, list):
            self._fetch_results = list(fetch_result)
        else:
            self._fetch_results = [fetch_result]
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchone(self):
        if not self._fetch_results:
            return None
        return self._fetch_results.pop(0)


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class PureHelpers(unittest.TestCase):
    def test_norm_fold_empty_to_none(self):
        self.assertIsNone(drdb._norm_fold(None))
        self.assertIsNone(drdb._norm_fold(""))
        self.assertIsNone(drdb._norm_fold("   "))
        self.assertEqual(drdb._norm_fold("paperA"), "paperA")

    def test_key_clause_null_vs_value(self):
        self.assertIn("fold_name IS NULL", drdb._key_clause(None))
        self.assertIn("fold_name=%(fold_name)s", drdb._key_clause("paperA"))

    def test_trunc(self):
        self.assertEqual(drdb._trunc(None, 5), "")
        self.assertEqual(drdb._trunc("abcdef", 3), "abc")


class ChushenInsert(unittest.TestCase):
    def test_insert_sets_status_zero_and_null_fold(self):
        cur = FakeCursor(fetch_result=None)  # no existing row -> INSERT
        conn = FakeConn(cur)
        findings = {"paper": {"doi": "10.1/x", "title": "T", "authors_full": [], "affiliations": []}}

        with mock.patch.object(drdb.pymssql, "connect", return_value=conn):
            drdb.upsert_chushen(
                submission_no="file123", fold_name="", task_id="task1",
                findings=findings, chushen_result="高风险",
                chushen_report_url="http://x/r.pdf",
            )

        self.assertTrue(conn.committed)
        self.assertTrue(conn.closed)
        select_sql = cur.statements[0][0]
        insert_sql = cur.statements[1][0]
        self.assertIn("fold_name IS NULL", select_sql)       # single-paper -> NULL key
        self.assertIn("INSERT INTO detection_reports", insert_sql)
        self.assertIn("status", insert_sql)
        # status=0 is appended as a literal in the INSERT VALUES list
        self.assertRegex(insert_sql, r"0\s*\)\s*$")
        self.assertIsNone(cur.statements[1][1]["fold_name"])

    def test_existing_row_updates_and_resets_review(self):
        cur = FakeCursor(fetch_result=(42,))  # existing row -> UPDATE
        conn = FakeConn(cur)
        findings = {"paper": {"doi": "10.1/x", "title": "T", "authors_full": [], "affiliations": []}}

        with mock.patch.object(drdb.pymssql, "connect", return_value=conn):
            drdb.upsert_chushen(
                submission_no="file123", fold_name="paperA", task_id="task1",
                findings=findings, chushen_result="低风险",
                chushen_report_url="http://x/r.pdf",
            )

        update_sql = cur.statements[1][0]
        self.assertIn("UPDATE detection_reports", update_sql)
        self.assertIn("status=0", update_sql)
        # re-submitting resets the 复审 fields so the row is back to first-pass state
        self.assertIn("review_result=NULL", update_sql)
        self.assertIn("fold_name=%(fold_name)s", update_sql)

    def test_verify_chushen_write_reads_back_url_and_status(self):
        cur = FakeCursor(fetch_result=[("http://x/r.pdf", 0)])
        conn = FakeConn(cur)

        with mock.patch.object(drdb.pymssql, "connect", return_value=conn):
            ok, reason = drdb.verify_chushen_write(
                submission_no="file123",
                fold_name="",
                chushen_report_url="http://x/r.pdf",
            )

        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertIn("SELECT chushen_report_url, status", cur.statements[0][0])

    def test_verify_chushen_write_rejects_stale_url(self):
        cur = FakeCursor(fetch_result=[("http://x/old.pdf", 0)])
        conn = FakeConn(cur)

        with mock.patch.object(drdb.pymssql, "connect", return_value=conn):
            ok, reason = drdb.verify_chushen_write(
                submission_no="file123",
                fold_name="paperA",
                chushen_report_url="http://x/r.pdf",
            )

        self.assertFalse(ok)
        self.assertIn("URL", reason)


class ReviewUpdate(unittest.TestCase):
    def test_review_sets_status_two(self):
        cur = FakeCursor()
        cur.rowcount = 1
        conn = FakeConn(cur)

        with mock.patch.object(drdb.pymssql, "connect", return_value=conn):
            matched = drdb.update_review(
                submission_no="file123", fold_name=None,
                review_result="高风险", review_report_url="http://x/rev.pdf",
            )

        self.assertTrue(matched)
        sql = cur.statements[0][0]
        self.assertIn("UPDATE detection_reports", sql)
        self.assertIn("status=2", sql)
        self.assertIn("fold_name IS NULL", sql)

    def test_review_returns_false_when_no_row(self):
        cur = FakeCursor()
        cur.rowcount = 0
        conn = FakeConn(cur)
        with mock.patch.object(drdb.pymssql, "connect", return_value=conn):
            matched = drdb.update_review(
                submission_no="missing", fold_name="paperZ",
                review_result="高风险", review_report_url="http://x/rev.pdf",
            )
        self.assertFalse(matched)

    def test_verify_review_write_reads_back_result_url_and_status(self):
        cur = FakeCursor(fetch_result=[("高风险", "http://x/rev.pdf", 2)])
        conn = FakeConn(cur)

        with mock.patch.object(drdb.pymssql, "connect", return_value=conn):
            ok, reason = drdb.verify_review_write(
                submission_no="file123",
                fold_name=None,
                review_result="高风险",
                review_report_url="http://x/rev.pdf",
            )

        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertIn("SELECT review_result, review_report_url, status", cur.statements[0][0])

    def test_verify_review_write_rejects_wrong_status(self):
        cur = FakeCursor(fetch_result=[("高风险", "http://x/rev.pdf", 0)])
        conn = FakeConn(cur)

        with mock.patch.object(drdb.pymssql, "connect", return_value=conn):
            ok, reason = drdb.verify_review_write(
                submission_no="file123",
                fold_name=None,
                review_result="高风险",
                review_report_url="http://x/rev.pdf",
            )

        self.assertFalse(ok)
        self.assertIn("status", reason)


class FakeTaskManager:
    def __init__(self):
        self.updates = []

    def update_task(self, task_id, **kwargs):
        self.updates.append((task_id, kwargs))


class DetectionReportsWorkerLifecycle(unittest.IsolatedAsyncioTestCase):
    def test_review_report_slug_falls_back_to_task_paper_key_without_doi(self):
        import api.services.detection_reports_worker as worker

        paper = PaperInfo(
            doi_slug="task_20260605-abcd",
            doi="",
            submission_no="sub123",
            fold_name="中文文献",
        )

        self.assertEqual(worker._review_report_slug(paper, {"doi": ""}), "task_20260605-abcd")
        self.assertTrue(
            worker._review_report_url_for_paper(paper, {"doi": ""}, "detection_reports/task1")
            .endswith("/review_task_20260605-abcd.pdf")
        )

    def test_review_doi_prefers_paper_doi_over_agent_doi(self):
        import api.services.detection_reports_worker as worker

        paper = PaperInfo(
            doi_slug="10.1038_s41467-026-71010-0",
            doi="10.1038/s41467-026-71010-0",
        )
        review = {
            "doi": "10.1038/s41467-026-71010-0 (note: agent added commentary)"
        }

        self.assertEqual(worker._review_doi(paper, review), "10.1038/s41467-026-71010-0")

    def test_chushen_fallback_uses_task_key_not_unknown(self):
        import api.services.detection_reports_worker as worker

        paper = PaperInfo(
            doi_slug="task_20260605-abcd",
            doi="",
            report_json="",
            submission_no="sub123",
            fold_name="",
        )

        self.assertTrue(
            worker._chushen_report_url_from_paper(paper, "detection_reports/task1")
            .endswith("/task_20260605-abcd.pdf")
        )

    async def test_chushen_db_failure_keeps_lowrisk_task_failed_not_completed(self):
        import api.services.detection_reports_worker as worker

        task_id = "20260605-testlow"
        paper = PaperInfo(
            doi_slug="10.1_x",
            doi="10.1/x",
            input_dir="/tmp/input",
            output_dir="/tmp/output",
            submission_no="sub123",
            fold_name="",
        )
        findings = {
            "pdf_generated": True,
            "paper": {"doi": "10.1/x", "title": "T", "authors_full": [], "affiliations": []},
            "summary": {},
        }
        tm = FakeTaskManager()

        with (
            mock.patch.object(worker, "run_detection_single", mock.AsyncMock(return_value=findings)),
            mock.patch.object(worker, "upsert_chushen", side_effect=RuntimeError("db down")),
            mock.patch("modules.chinese_report_generator._compute_overall_risk", return_value={"level": "低风险"}),
        ):
            await worker._run_inner(task_id, tm, [paper], {"max_workers": 1})

        final_update = tm.updates[-1][1]
        self.assertEqual(final_update["status"], TaskStatus.FAILED)
        self.assertIn("Detection/initial report persistence failed", final_update["error"])
        self.assertEqual(final_update["result"]["detected_fail"], 1)
        self.assertEqual(paper.status, "error")

    async def test_chushen_verify_failure_keeps_task_failed_not_completed(self):
        import api.services.detection_reports_worker as worker

        task_id = "20260605-testverify"
        paper = PaperInfo(
            doi_slug="10.1_x",
            doi="10.1/x",
            input_dir="/tmp/input",
            output_dir="/tmp/output",
            submission_no="sub123",
            fold_name="",
        )
        findings = {
            "pdf_generated": True,
            "paper": {"doi": "10.1/x", "title": "T", "authors_full": [], "affiliations": []},
            "summary": {},
        }
        tm = FakeTaskManager()

        with (
            mock.patch.object(worker, "run_detection_single", mock.AsyncMock(return_value=findings)),
            mock.patch.object(worker, "upsert_chushen", return_value=None),
            mock.patch.object(worker, "verify_chushen_write", return_value=(False, "stale URL")),
            mock.patch("modules.chinese_report_generator._compute_overall_risk", return_value={"level": "低风险"}),
        ):
            await worker._run_inner(task_id, tm, [paper], {"max_workers": 1})

        final_update = tm.updates[-1][1]
        self.assertEqual(final_update["status"], TaskStatus.FAILED)
        self.assertIn("Detection/initial report persistence failed", final_update["error"])
        self.assertIn("Initial report DB verification failed", paper.error)

    async def test_review_db_miss_keeps_task_failed_not_completed(self):
        import api.services.detection_reports_worker as worker
        from modules.chinese_report_generator import doi_to_slug

        task_id = "20260605-testdb"
        paper = PaperInfo(
            doi_slug="10.1_x",
            doi="10.1/x",
            input_dir="/tmp/input",
            output_dir="/tmp/output",
            submission_no="sub123",
            fold_name="",
        )
        findings = {
            "pdf_generated": True,
            "paper": {"doi": "10.1/x", "title": "T", "authors_full": [], "affiliations": []},
            "summary": {},
        }
        review = {
            "doi": "10.1/x",
            "result": "高风险",
            "verdict": "建议高风险",
            "trigger": "data",
            "image_review": "未发现图像异常。",
            "data_review": "数据存在整块错配。",
            "ref_review": "参考文献未发现异常。",
            "reason": "数据维度存在整块错配。",
        }
        namespace = f"detection_reports/{task_id}"
        expected_pdf = str(worker.REVIEW_DIR / namespace / f"review_{doi_to_slug('10.1/x')}.pdf")
        tm = FakeTaskManager()

        with (
            mock.patch.object(worker, "run_detection_single", mock.AsyncMock(return_value=findings)),
            mock.patch.object(worker, "run_review_single", mock.AsyncMock(return_value=review)),
            mock.patch.object(worker, "generate_reports", mock.AsyncMock(return_value=[expected_pdf])),
            mock.patch.object(worker, "upsert_chushen", return_value=None),
            mock.patch.object(worker, "verify_chushen_write", return_value=(True, "")),
            mock.patch.object(worker, "update_review", return_value=False),
            mock.patch("modules.chinese_report_generator._compute_overall_risk", return_value={"level": "高风险"}),
        ):
            await worker._run_inner(task_id, tm, [paper], {"max_workers": 1})

        final_update = tm.updates[-1][1]
        self.assertEqual(final_update["status"], TaskStatus.FAILED)
        self.assertIn("review_report_url is written", final_update["error"])
        self.assertEqual(final_update["result"]["persistence_failed"], 1)
        self.assertEqual(final_update["result"]["reviewed"], 0)
        self.assertEqual(final_update["result"]["detection_reports"][0]["review_result"], "高风险")
        self.assertEqual(final_update["result"]["detection_reports"][0]["review_error"],
                         "review DB row was not found; review_report_url was not persisted")
        self.assertEqual(paper.status, "persist_error")

    async def test_review_verify_failure_keeps_task_failed_not_completed(self):
        import api.services.detection_reports_worker as worker
        from modules.chinese_report_generator import doi_to_slug

        task_id = "20260605-testreviewverify"
        paper = PaperInfo(
            doi_slug="10.1_x",
            doi="10.1/x",
            input_dir="/tmp/input",
            output_dir="/tmp/output",
            submission_no="sub123",
            fold_name="",
        )
        findings = {
            "pdf_generated": True,
            "paper": {"doi": "10.1/x", "title": "T", "authors_full": [], "affiliations": []},
            "summary": {},
        }
        review = {
            "doi": "10.1/x",
            "result": "高风险",
            "verdict": "建议高风险",
            "trigger": "data",
            "image_review": "未发现图像异常。",
            "data_review": "数据存在整块错配。",
            "ref_review": "参考文献未发现异常。",
            "reason": "数据维度存在整块错配。",
        }
        namespace = f"detection_reports/{task_id}"
        expected_pdf = str(worker.REVIEW_DIR / namespace / f"review_{doi_to_slug('10.1/x')}.pdf")
        tm = FakeTaskManager()

        with (
            mock.patch.object(worker, "run_detection_single", mock.AsyncMock(return_value=findings)),
            mock.patch.object(worker, "run_review_single", mock.AsyncMock(return_value=review)),
            mock.patch.object(worker, "generate_reports", mock.AsyncMock(return_value=[expected_pdf])),
            mock.patch.object(worker, "upsert_chushen", return_value=None),
            mock.patch.object(worker, "verify_chushen_write", return_value=(True, "")),
            mock.patch.object(worker, "update_review", return_value=True),
            mock.patch.object(worker, "verify_review_write", return_value=(False, "stale review URL")),
            mock.patch("modules.chinese_report_generator._compute_overall_risk", return_value={"level": "高风险"}),
        ):
            await worker._run_inner(task_id, tm, [paper], {"max_workers": 1})

        final_update = tm.updates[-1][1]
        self.assertEqual(final_update["status"], TaskStatus.FAILED)
        self.assertIn("Report DB persistence failed", final_update["error"])
        self.assertEqual(final_update["result"]["persistence_failed"], 1)
        self.assertIn("review DB verification failed", final_update["result"]["detection_reports"][0]["review_error"])
        self.assertEqual(paper.status, "persist_error")


if __name__ == "__main__":
    unittest.main()
