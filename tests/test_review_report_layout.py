import fitz
import pytest
from PIL import Image

from api.services.review_validation import renderer_module


@pytest.mark.parametrize('size', [(200, 2400), (3000, 220), (1500, 1400)])
@pytest.mark.parametrize('paragraphs', [1, 15, 25])
def test_evidence_keeps_aspect_ratio_and_caption_clear(tmp_path, size, paragraphs):
    source = tmp_path / 'evidence.png'
    Image.new('RGB', size, '#ddeeff').save(source)
    renderer = renderer_module()
    evidence = renderer._evidence_images_html([{'path': source, 'caption': 'CAPTION after image'}])
    path = tmp_path / 'report.pdf'
    renderer._render_pdf('<body>' + '<p>Before</p>'*paragraphs + evidence + '<p>After</p></body>', str(path))
    with fitz.open(path) as doc:
        assert 'CAPTION after image' in ''.join(p.get_text() for p in doc)
        images = [(p, fitz.Rect(im['bbox'])) for p in doc for im in p.get_image_info()]
        assert len(images) == 1
        page, rect = images[0]
        assert 'CAPTION after image' in page.get_text()
        assert rect.width / rect.height == pytest.approx(size[0]/size[1], rel=0.002)
        assert rect.width <= 425.1 and rect.height <= 370.1
        for block in page.get_text('blocks'):
            if block[6] == 0:
                overlap = fitz.Rect(block[:4]) & rect
                assert overlap.is_empty, (block[4], overlap)
