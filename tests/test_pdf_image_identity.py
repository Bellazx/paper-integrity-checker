from pathlib import Path

from utils.pdf_utils import _pdf_output_prefix, _pdf_source_id


def test_pdf_image_prefix_is_stable_and_source_specific(tmp_path: Path):
    main_pdf = tmp_path / "main" / "paper.pdf"
    supplement_pdf = tmp_path / "supplement" / "paper.pdf"

    assert _pdf_output_prefix(str(main_pdf)) == _pdf_output_prefix(str(main_pdf))
    assert _pdf_output_prefix(str(main_pdf)) != _pdf_output_prefix(str(supplement_pdf))
    assert _pdf_source_id(str(main_pdf)) != _pdf_source_id(str(supplement_pdf))
