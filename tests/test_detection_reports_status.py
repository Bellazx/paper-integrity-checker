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


class FakeCursor:
    def __init__(self, fetch_result=None):
        self.statements = []          # list of (sql, params)
        self._fetch_result = fetch_result
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchone(self):
        return self._fetch_result


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


if __name__ == "__main__":
    unittest.main()
