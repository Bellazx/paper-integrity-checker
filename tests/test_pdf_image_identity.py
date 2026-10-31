from pathlib import Path

from PIL import Image

from utils.pdf_utils import _pdf_output_prefix, _pdf_source_id, extract_standalone_images


def test_pdf_image_prefix_is_stable_and_source_specific(tmp_path: Path):
    main_pdf = tmp_path / "main" / "paper.pdf"
    supplement_pdf = tmp_path / "supplement" / "paper.pdf"

    assert _pdf_output_prefix(str(main_pdf)) == _pdf_output_prefix(str(main_pdf))
    assert _pdf_output_prefix(str(main_pdf)) != _pdf_output_prefix(str(supplement_pdf))
    assert _pdf_source_id(str(main_pdf)) != _pdf_source_id(str(supplement_pdf))


def test_standalone_gif_images_are_included(tmp_path: Path):
    paper_dir = tmp_path / "paper"
    output_dir = tmp_path / "output"
    figure_dir = paper_dir / "figure"
    figure_dir.mkdir(parents=True)

    gif_path = figure_dir / "figure1.gif"
    Image.new("RGB", (240, 220), color=(20, 120, 200)).save(gif_path)

    images = extract_standalone_images(str(paper_dir), str(output_dir), max_images=10)

    assert len(images) == 1
    assert images[0].filepath == str(gif_path)
    assert images[0].width == 240
    assert images[0].height == 220


def test_stale_output_images_are_not_reread_as_standalone(tmp_path: Path):
    # 10.1038/s41467-026-77281-x: an interrupted run left 29 supplementary-PDF
    # extractions in output/images; the retry read them back as page-0
    # standalone images and dropped the real supplementary extraction as dups.
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    images_dir = tmp_path / "output" / "images"
    images_dir.mkdir(parents=True)
    Image.new("RGB", (240, 220), color=(200, 20, 20)).save(images_dir / "supp_page_003_img_000.png")
    docx_image = images_dir / "si_docx_image1.png"
    Image.new("RGB", (240, 220), color=(20, 200, 20)).save(docx_image)

    images = extract_standalone_images(str(paper_dir), str(tmp_path / "output"), max_images=10,
                                       extra_image_files=[str(docx_image)])

    assert [img.filepath for img in images] == [str(docx_image)]
