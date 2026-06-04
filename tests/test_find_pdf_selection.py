from pathlib import Path

from core.pipeline import find_pdf


def _touch(path: Path, size: int = 1024) -> None:
    path.write_bytes(b"x" * size)


def test_find_pdf_prefers_manuscript_over_peer_review(tmp_path):
    paper_dir = tmp_path / "10.1038_s41467-026-74176-9"
    paper_dir.mkdir()
    _touch(paper_dir / "peer_review.pdf", size=5_000_000)
    manuscript = paper_dir / "Manuscript.pdf"
    _touch(manuscript, size=1_000_000)

    assert find_pdf(str(paper_dir)) == str(manuscript)


def test_find_pdf_prefers_directory_named_pdf_over_supplement(tmp_path):
    paper_dir = tmp_path / "10.1038_s41467-026-74176-9"
    paper_dir.mkdir()
    main_pdf = paper_dir / "10.1038_s41467-026-74176-9.pdf"
    _touch(main_pdf, size=1_000_000)
    _touch(paper_dir / "supplementary.pdf", size=2_000_000)

    assert find_pdf(str(paper_dir)) == str(main_pdf)


def test_find_pdf_avoids_response_when_article_exists(tmp_path):
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    _touch(paper_dir / "response_to_reviewers.pdf", size=2_000_000)
    article = paper_dir / "article.pdf"
    _touch(article, size=1_000_000)

    assert find_pdf(str(paper_dir)) == str(article)
