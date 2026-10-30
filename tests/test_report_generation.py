from api.services.report import _rendered_pdf_names


def test_renderer_pdf_markers_are_used_for_generation_accounting():
    output = """
    [PDF] review_10.1000_example.pdf
    copied [PDF] not-a-marker.pdf
    [PDF] review_second.pdf  
    """
    assert _rendered_pdf_names(output) == {
        "review_10.1000_example.pdf",
        "review_second.pdf",
    }


def test_renderer_pdf_marker_parser_handles_empty_output():
    assert _rendered_pdf_names("") == set()
