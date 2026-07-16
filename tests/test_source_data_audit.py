from pathlib import Path
import unittest

from api.services.source_data_audit import audit_source_data


FIXTURE_ROOT = Path("/home/sjtu/paper-integrity-checker/data/input/scholar/0705gengtongxue")


class SourceDataAuditRegressionTests(unittest.TestCase):
    def test_detects_s41586_022_matrix_reuse_and_n_mismatch(self):
        result = audit_source_data(FIXTURE_ROOT / "10.1038_s41586-022-04888-7", doi="10.1038/s41586-022-04888-7")

        matrix_locations = "\n".join(item["location"] for item in result["rectangular_matrix_reuse"])
        self.assertIn("41586_2022_4888_MOESM4_ESM.xlsx / Fig. 2d", matrix_locations)
        self.assertIn("41586_2022_4888_MOESM7_ESM.xlsx / Extended Data Fig. 3d", matrix_locations)
        self.assertTrue(any(item["n_values"] >= 15 for item in result["rectangular_matrix_reuse"]))

        n_locations = "\n".join(item["location"] for item in result["figure_n_mismatch"])
        self.assertIn("41586_2022_4888_MOESM5_ESM.xlsx / Fig. 3e / Vehicle", n_locations)
        self.assertIn("41586_2022_4888_MOESM5_ESM.xlsx / Fig. 3e / TMX", n_locations)
        self.assertTrue(any(item["declared_n"] == 3 and item["source_n"] == 4 for item in result["figure_n_mismatch"]))


    def test_detects_s41586_025_extended_data_rtqpcr_reuse(self):
        result = audit_source_data(FIXTURE_ROOT / "10.1038_s41586-025-09574-y", doi="10.1038/s41586-025-09574-y")

        locations = "\n".join(item["location"] for item in result["rectangular_matrix_reuse"])
        self.assertIn("41586_2025_9574_MOESM12_ESM.xlsx / Extended data fig.2 b", locations)
        self.assertIn("41586_2025_9574_MOESM19_ESM.xlsx / Extended data fig.11 b", locations)
        self.assertGreaterEqual(sum(1 for item in result["rectangular_matrix_reuse"] if item["n_values"] >= 21), 2)
        self.assertTrue(result["high_findings"])


if __name__ == "__main__":
    unittest.main()
