import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import batch_rereview


class BatchRereviewBudgetTests(unittest.TestCase):
    def test_can_start_more_without_deadline(self):
        self.assertTrue(batch_rereview._can_start_more(None, 300))

    def test_can_start_more_before_margin(self):
        with mock.patch.object(batch_rereview.time, "monotonic", return_value=1000.0):
            self.assertTrue(batch_rereview._can_start_more(1400.0, 300.0))

    def test_stops_when_inside_margin(self):
        with mock.patch.object(batch_rereview.time, "monotonic", return_value=1000.0):
            self.assertFalse(batch_rereview._can_start_more(1200.0, 300.0))


if __name__ == "__main__":
    unittest.main()
