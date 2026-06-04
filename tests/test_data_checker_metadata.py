import tempfile
import unittest
from pathlib import Path

import pandas as pd

from core.pipeline import find_data_dir
from modules.data_checker import _analyze_sheet, _load_data_files, check_data_anomalies


VALID_PDB = """\
ATOM      1  N   GLY A   1      11.104  13.207   2.333  1.00 20.00           N
ATOM      2  CA  GLY A   1      12.233  13.991   2.701  1.00 21.00           C
ATOM      3  C   GLY A   1      13.104  13.207   3.555  1.00 22.00           C
ATOM      4  O   GLY A   1      14.204  13.607   3.755  1.00 23.00           O
END
"""

BAD_PDB = """\
ATOM      1  N   GLY A   1      11.104  13.207   2.333  1.00 20.00           N
ATOM      2  N   GLY A   1      11.104  13.207   2.333  1.00 20.00           N
ATOM      3  CA  GLY A   1      11.104  13.207   2.333  1.50 21.00           C
ATOM      4  C   GLY A   1       0.000   0.000   0.000  1.00 22.00           C
ATOM      5  O   GLY A   1       0.000   0.000   0.000  1.00 -3.00           O
ATOM      6  H   GLY A   1       0.000   0.000   0.000  1.00 22.00           H
END
"""


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

    def test_pdb_file_marks_directory_as_data_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "supplement materials").mkdir()
            (root / "supplement materials" / "structure.pdb").write_text(VALID_PDB, encoding="utf-8")

            self.assertEqual(find_data_dir(str(root)), str(root))

    def test_valid_pdb_does_not_emit_integrity_anomaly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "structure.pdb").write_text(VALID_PDB, encoding="utf-8")

            anomalies = check_data_anomalies(str(root))

            self.assertEqual(anomalies, [])

    def test_pdb_integrity_anomalies_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "extended data").mkdir()
            (root / "extended data" / "bad_structure.pdb").write_text(BAD_PDB, encoding="utf-8")

            anomalies = check_data_anomalies(str(root))
            tests = {a.get("test") for a in anomalies}

            self.assertIn("pdb_duplicate_atom_identity", tests)
            self.assertIn("pdb_duplicate_coordinates", tests)
            self.assertIn("pdb_invalid_occupancy", tests)
            self.assertIn("pdb_zero_coordinates", tests)

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
