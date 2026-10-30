from pathlib import Path

from modules.data_checker import _load_data_files, _read_delimited_text_xls


def test_reads_tab_delimited_text_with_xls_suffix(tmp_path: Path) -> None:
    path = tmp_path / "source.xls"
    path.write_text("group\tvalue\tpercent\nA\t8\t8.51\nB\t3\t3.19\n", encoding="utf-8")

    raw = _read_delimited_text_xls(path)
    loaded, failed, _ = _load_data_files(tmp_path)

    assert raw is not None
    assert raw["Sheet1"].shape == (3, 3)
    assert "source.xls" in loaded
    assert failed == []


def test_does_not_treat_binary_or_plain_text_as_delimited_xls(tmp_path: Path) -> None:
    binary = tmp_path / "binary.xls"
    binary.write_bytes(b"\xd0\xcf\x11\xe0\x00\x00broken")
    plain = tmp_path / "plain.xls"
    plain.write_text("not a table\nstill not a table\n", encoding="utf-8")

    assert _read_delimited_text_xls(binary) is None
    assert _read_delimited_text_xls(plain) is None
