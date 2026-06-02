"""Custom-table writer (_insert_to_custom_table) parity with the main table path.

#1: it must (a) build bibliographic metadata via the shared build_paper_metadata
(not hardcode department="" / author_type="通讯作者"), and (b) honor the
protected_snapshot_2054.json lock on UPDATE, like utils.db.insert_findings.

The DB is replaced with a fake connection that records SQL + params; no SQL Server
is touched. build_paper_metadata is patched to a sentinel so we can prove it's used.

Run:  python3 -m unittest tests.test_custom_table_insert
"""
import os
import sys
import json
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.services.detect as D


class FakeCursor:
    def __init__(self, fetch_result=None):
        self.statements = []
        self._fetch_result = fetch_result

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


_SENTINEL_META = {
    "title": "T", "author": "张三", "author_type": "第一作者",
    "department": "电子信息与电气工程学院", "author_all": "张三, 李四",
    "department_all": "1. SJTU", "journal": "J", "doi": "10.1/x",
}

FINDINGS = {
    "paper": {"doi": "10.1/x", "title": "T", "total_pages": 1, "total_images": 2,
              "total_references": 3, "authors_full": ["张三", "李四"], "affiliations": ["1. SJTU"]},
    "image_duplicates": [], "image_splicing": [], "data_anomalies": [], "reference_issues": [],
    "summary": {"data_issues": 0},
}


def _patch_risk(stack):
    """Patch the risk helpers so the test doesn't depend on scoring internals."""
    fake_dim = {"score": 0, "level": "低风险", "color": "#080", "high": 0, "medium": 0, "low": 0}
    fake_overall = {"score": 0, "level": "低风险", "color": "#080", "high_dimensions": set()}
    import modules.chinese_report_generator as C
    stack.enter_context(mock.patch.object(C, "_compute_dimension_risk", return_value=dict(fake_dim)))
    stack.enter_context(mock.patch.object(C, "_compute_image_risk", return_value=dict(fake_dim)))
    stack.enter_context(mock.patch.object(C, "_compute_overall_risk", return_value=dict(fake_overall)))
    stack.enter_context(mock.patch.object(C, "_apply_data_caps", side_effect=lambda x: x))


class UsesSharedMetadata(unittest.TestCase):
    def test_insert_uses_build_paper_metadata_not_hardcoded(self):
        cur = FakeCursor(fetch_result=None)  # no existing row -> INSERT
        conn = FakeConn(cur)
        import contextlib
        with contextlib.ExitStack() as stack:
            _patch_risk(stack)
            stack.enter_context(mock.patch("utils.db.build_paper_metadata", return_value=dict(_SENTINEL_META)))
            stack.enter_context(mock.patch("pymssql.connect", return_value=conn))
            D._insert_to_custom_table(FINDINGS, "yujing_test", "")

        insert_params = cur.statements[-1][1]
        # department + author_type come from build_paper_metadata, NOT the old hardcodes
        self.assertEqual(insert_params["department"], "电子信息与电气工程学院")
        self.assertEqual(insert_params["author_type"], "第一作者")
        self.assertEqual(insert_params["author"], "张三")
        self.assertNotEqual(insert_params["department"], "")


class ProtectedSnapshotGuard(unittest.TestCase):
    def test_protected_doi_skips_update(self):
        cur = FakeCursor(fetch_result=(1,))  # existing row -> would UPDATE
        conn = FakeConn(cur)
        import contextlib
        with contextlib.ExitStack() as stack:
            _patch_risk(stack)
            stack.enter_context(mock.patch("utils.db.build_paper_metadata", return_value=dict(_SENTINEL_META)))
            stack.enter_context(mock.patch("pymssql.connect", return_value=conn))
            # point MAIN_PY.parent/data/protected_snapshot_2054.json at a temp file listing our DOI
            tmp = tempfile.mkdtemp()
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            with open(os.path.join(tmp, "data", "protected_snapshot_2054.json"), "w") as f:
                json.dump(["10.1/x"], f)
            fake_main = mock.MagicMock()
            fake_main.parent = __import__("pathlib").Path(tmp)
            stack.enter_context(mock.patch.object(D, "MAIN_PY", fake_main))
            D._insert_to_custom_table(FINDINGS, "yujing_test", "")

        # only the SELECT ran; no UPDATE/INSERT statement was issued
        sqls = " ".join(s[0] for s in cur.statements)
        self.assertIn("SELECT", sqls)
        self.assertNotIn("UPDATE", sqls)
        self.assertNotIn("INSERT", sqls)
        self.assertTrue(conn.closed)

    def test_unprotected_doi_updates(self):
        cur = FakeCursor(fetch_result=(1,))
        conn = FakeConn(cur)
        import contextlib
        with contextlib.ExitStack() as stack:
            _patch_risk(stack)
            stack.enter_context(mock.patch("utils.db.build_paper_metadata", return_value=dict(_SENTINEL_META)))
            stack.enter_context(mock.patch("pymssql.connect", return_value=conn))
            tmp = tempfile.mkdtemp()  # no snapshot file at all
            fake_main = mock.MagicMock()
            fake_main.parent = __import__("pathlib").Path(tmp)
            stack.enter_context(mock.patch.object(D, "MAIN_PY", fake_main))
            D._insert_to_custom_table(FINDINGS, "yujing_test", "")

        sqls = " ".join(s[0] for s in cur.statements)
        self.assertIn("UPDATE yujing_test", sqls)
        self.assertTrue(conn.committed)


if __name__ == "__main__":
    unittest.main()
