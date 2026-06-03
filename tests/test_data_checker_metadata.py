import tempfile
import unittest
from pathlib import Path

import pandas as pd

from core.pipeline import find_data_dir
from modules.data_checker import _analyze_sheet, _load_data_files, check_data_anomalies


class DataCheckerMetadataTests(unittest.TestCase):
    def test_skips_crawler_metadata_csv_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "resources.csv").write_text(
                "label,size,figure_no\nA,100,1\nB,200,2\n", encoding="utf-8"
            )
            (root / "figures.csv").write_text(
                "figure_no,page,width\n1,3,1200\n2,4,1200\n", encoding="utf-8"
            )

            loaded, failed = _load_data_files(str(root))

            self.assertEqual(loaded, {})
            self.assertEqual(failed, [])
            self.assertIsNone(find_data_dir(str(root)))
            self.assertEqual(check_data_anomalies(str(root)), [])

    def test_loads_real_source_data_next_to_metadata_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "resources.csv").write_text(
                "label,size,figure_no\nA,100,1\nB,200,2\n", encoding="utf-8"
            )
            pd.DataFrame({"group_a": [1.1, 1.2, 1.3], "group_b": [2.1, 2.2, 2.3]}).to_excel(
                root / "source_data.xlsx", index=False
            )

            loaded, failed = _load_data_files(str(root))

            self.assertEqual(set(loaded), {"source_data.xlsx"})
            self.assertEqual(failed, [])
            self.assertEqual(find_data_dir(str(root)), str(root))

    def test_skips_duplicate_source_data_files_by_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            df = pd.DataFrame({"group_a": [1.1, 1.2, 1.3], "group_b": [2.1, 2.2, 2.3]})
            first = root / "source_data.xlsx"
            second = root / "10.1234__paper__source_data.xlsx"
            df.to_excel(first, index=False)
            second.write_bytes(first.read_bytes())

            loaded, failed = _load_data_files(str(root))

            self.assertEqual(len(loaded), 1)
            self.assertEqual(failed, [])

    def test_non_measurement_columns_do_not_drive_high_risk(self):
        df = pd.DataFrame({
            "Sample number": list(range(1, 31)),
            "Blank": [0.0] * 30,
            "F6L": [0.0] * 30,
            "viral eVal": [1e-5] * 30,
            "Phylum count": [1, 2, 1, 3, 2] * 6,
            "col_20": [5.0] * 30,
            "col_21": [5.0] * 30,
        })

        anomalies = _analyze_sheet(df, "source.xlsx", "Fig. 1")

        self.assertFalse(any(a.get("severity") == "high" for a in anomalies), anomalies)
        self.assertFalse(any(a.get("test") == "cross_group_duplicate" for a in anomalies), anomalies)

    def test_named_measurement_columns_are_still_checked(self):
        df = pd.DataFrame({
            "tumor_volume_a": [1.1, 1.2, 1.3, 1.4, 1.1] * 12,
            "tumor_volume_b": [2.1, 2.2, 2.3, 2.4, 2.1] * 12,
        })

        anomalies = _analyze_sheet(df, "source.xlsx", "Table 1")

        self.assertTrue(any(a.get("test") == "value_recycling" for a in anomalies), anomalies)

    def test_snp_tree_matrix_is_not_treated_as_measurement_data(self):
        df = pd.DataFrame({
            "Sample number": list(range(1, 118)),
            "1.0": list(range(1, 118)),
            "2.0": [x - 16 for x in range(1, 118)],
            "3.0": [x - 12 for x in range(1, 118)],
        })

        anomalies = _analyze_sheet(df, "source.xlsx", "Figure 2 SNPs used for tree construction")

        self.assertEqual(anomalies, [])


if __name__ == "__main__":
    unittest.main()
