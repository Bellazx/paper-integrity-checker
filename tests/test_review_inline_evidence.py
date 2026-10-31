import copy
import json
import importlib.util
import unicodedata

import fitz
import pytest
from PIL import Image

from api.services.review_validation import renderer_module, renderer_path
from modules.review_report_layout import bind_issue_evidence


def sample(tmp_path):
    images = []
    # Deliberately reverse evidence input order. Position must follow the
    # explicit issue mapping, not the incidental list order.
    for index in (2, 1):
        path = tmp_path / f'image{index}.png'
        Image.new('RGB', (1200, 650), 'red' if index == 1 else 'blue').save(path)
        images.append({'dimension': 'data', 'issue_index': index,
                       'path': str(path), 'caption': f'CAPTION_{index}', 'max_height': 200})
    return {'result': '高风险', 'verdict': '建议高风险',
            'dimension_verdicts': {'image': '建议低风险', 'data': '建议高风险',
                                   'ref': '建议低风险', 'methodology': '建议低风险'},
            'image_review': 'No image issue identified in Figures 1-3.',
            'data_review': '(1) ISSUE_ONE: Table 1 reports 3 versus 5.\n\n'
                           '(2) ISSUE_TWO: Table 2 reports 8 versus 9.',
            'ref_review': 'References 1-3 match their titles.',
            'methodology_review': 'Methods and legends report n=5.',
            'reason': 'High Risk Recommended. The original report identifies Table 1 and Table 2.',
            'evidence_images': images}


def check_pdf(path):
    with fitz.open(path) as pdf:
        text = unicodedata.normalize('NFKC', '\n'.join(page.get_text() for page in pdf))
        assert text.index('ISSUE_ONE') < text.index('CAPTION_1') < text.index('ISSUE_TWO') < text.index('CAPTION_2')
        assert sum(len(page.get_image_info()) for page in pdf) == 2
        assert '\ufffd' not in text
        for page in pdf:
            for image in page.get_image_info():
                rect = fitz.Rect(image['bbox'])
                assert rect.height <= 200.1
                assert rect.width / rect.height == pytest.approx(1200/650, rel=.002)
                for block in page.get_text('blocks'):
                    if block[6] == 0:
                        assert (rect & fitz.Rect(block[:4])).is_empty
        return text


@pytest.mark.parametrize('entry', ['api', 'workflow'])
def test_chinese_issue_image_order_in_actual_pdf(tmp_path, entry):
    if entry == 'api':
        renderer = renderer_module()
    else:
        spec = importlib.util.spec_from_file_location('inline_workflow', renderer_path())
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
    review = sample(tmp_path)
    original = copy.deepcopy(review)
    images = renderer._review_evidence_images(review, {}, tmp_path / 'images')
    document = '<body>' + renderer._analysis_html(review, images) + '</body>'
    output = tmp_path / 'zh.pdf'
    renderer._render_pdf(document, str(output))
    check_pdf(output)
    assert review == original


def test_english_uses_same_crops_and_public_wording(tmp_path, monkeypatch):
    from modules import english_report_generator as english
    review = sample(tmp_path)
    original = copy.deepcopy(review)
    # Isolate translation availability; exercise the real evidence collector,
    # paragraph/image order, fonts and PDF renderer without network requests.
    monkeypatch.setattr(english, 'chat', lambda prompt, **kw: prompt.split('INPUT:\n', 1)[1])
    monkeypatch.setattr(english, '_paper_from_review', lambda review: {'title': 'Example'})
    output = english.generate_english_review_pdf(review, tmp_path, 'example')
    text = check_pdf(output)
    assert 'Risk identified' in text and 'No risk identified' in text
    for forbidden in ('High Risk', 'Low Risk', 'original report', 'Initial Screening Reference', '证据截图'):
        assert forbidden not in text
    assert review == original


def test_missing_translation_cannot_silently_drop_a_finding(tmp_path, monkeypatch):
    from modules import english_report_generator as english
    monkeypatch.setattr(english, 'chat', lambda *a, **kw: json.dumps({'reason': 'Example'}))
    with pytest.raises(ValueError, match='missing fields'):
        english.generate_english_review_pdf(sample(tmp_path), tmp_path, 'missing')
    assert not (tmp_path / 'review_missing_en.pdf').exists()


def test_multiple_images_per_issue_and_unbound_evidence():
    text = '(1) First\n(2) Second'
    _, issues = bind_issue_evidence(text, [{'issue_index': 2}, {'issue_index': 1}, {'issue_index': 1}])
    assert [len(item[2]) for item in issues] == [2, 1]
    for invalid in ({}, {'issue_index': 3}, {'issue_index': True}):
        with pytest.raises(ValueError, match='issue_index'):
            bind_issue_evidence(text, [invalid])


def test_chinese_issue_numbers_bind_evidence():
    text = '一、第一项问题。二、第二项问题。三、第三项问题。'
    prefix, issues = bind_issue_evidence(text, [
        {'dimension': 'data', 'issue_index': 1},
        {'dimension': 'data', 'issue_index': 2},
        {'dimension': 'data', 'issue_index': 3},
    ])
    assert prefix == ''
    assert [item[0] for item in issues] == [1, 2, 3]


def test_issue_evidence_accepts_string_and_ordered_indices():
    text = '(1) First finding\n(2) Second finding'
    _, issues = bind_issue_evidence(text, [
        {'dimension': 'data', 'issue_index': '1'},
        {'dimension': 'data', 'issue_index': '2'},
    ])
    assert [item[0] for item in issues] == [1, 2]
    _, issues = bind_issue_evidence(text, [
        {'dimension': 'data'},
        {'dimension': 'data'},
    ])
    assert [item[0] for item in issues] == [1, 2]


def test_explicit_evidence_not_truncated_to_four_images(tmp_path):
    renderer = renderer_module()
    review = sample(tmp_path)
    review['evidence_images'] = [dict(review['evidence_images'][0], issue_index=i) for i in range(1, 7)]
    collected = renderer._review_evidence_images(review, {}, tmp_path / 'images')
    assert [im['issue_index'] for im in collected] == list(range(1, 7))
    assert all(im['max_height'] == 200 for im in collected)


def test_english_binds_renderer_generated_evidence_without_index():
    from modules import english_report_generator as english

    text = '(1) First finding\n(2) Second finding'
    _, issues = english._bind_generated_evidence(
        text,
        [{'dimension': 'data', 'path': 'page.png'}],
        bind_issue_evidence,
    )
    assert issues[0][0] == 1
    assert issues[0][2][0]['issue_index'] == 1


def test_english_does_not_repair_explicit_invalid_evidence_index():
    from modules import english_report_generator as english

    with pytest.raises(ValueError, match='issue_index'):
        english._bind_generated_evidence(
            '(1) First finding',
            [{'dimension': 'data', 'issue_index': 9}],
            bind_issue_evidence,
        )
