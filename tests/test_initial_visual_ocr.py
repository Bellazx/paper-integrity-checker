from modules.initial_content_screen import read_pages


def test_image_page_ocr_is_added_as_supplementary_source(monkeypatch, tmp_path):
    import fitz
    from PIL import Image

    pdf = tmp_path / 'figure.pdf'
    image_path = tmp_path / 'image.png'
    Image.new('RGB', (80, 80), 'white').save(image_path)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(0, 0, 80, 80), filename=str(image_path))
    page.insert_text((10, 100), 'Figure 1 caption and table context ' * 5)
    doc.save(pdf)
    doc.close()

    monkeypatch.setattr('modules.initial_content_screen.shutil.which', lambda _: '/usr/bin/tesseract')

    class Result:
        stdout = b'Fig 1 0.62 0.93 1.19'
        stderr = b''

    monkeypatch.setattr('modules.initial_content_screen.subprocess.run', lambda *a, **k: Result())
    pages, warnings = read_pages(str(pdf))
    assert pages[0]['sources']['p1-ocr'].startswith('Fig 1')
    assert '图像文字OCR' in pages[0]['text']
    assert not any('OCR未完成' in warning for warning in warnings)
