"""verify_public_pdf must be able to fetch report URLs whose file name holds
the paper's title (Chinese or no-DOI papers)."""
import asyncio
import io
import urllib.request

from api.services import report_access


class _Response(io.BytesIO):
    status = 206

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_request_url_percent_encodes_non_ascii_path_only():
    url = 'http://202.120.57.100/reports/detection_reports/t1/unknown_资源服务一体化_071c2e9d.pdf'
    wire = report_access.request_url(url)
    wire.encode('ascii')  # what http.client needs
    assert wire == ('http://202.120.57.100/reports/detection_reports/t1/unknown_'
                    '%E8%B5%84%E6%BA%90%E6%9C%8D%E5%8A%A1%E4%B8%80%E4%BD%93%E5%8C%96_071c2e9d.pdf')
    ascii_url = 'http://202.120.57.100/reports/x/review_10.1038_s41467-026-77281-x.pdf'
    assert report_access.request_url(ascii_url) == ascii_url
    already = 'http://h/r/a%20b.pdf'
    assert report_access.request_url(already) == already


def test_verify_public_pdf_sends_encoded_url(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout=0):
        seen.append(request.full_url)
        return _Response(b'%PDF-1.7')

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    url = 'http://202.120.57.100/reports/detection_reports/t1/unknown_智能选书_a7962778.pdf'
    asyncio.run(report_access.verify_public_pdf(url))
    assert seen == ['http://202.120.57.100/reports/detection_reports/t1/unknown_%E6%99%BA%E8%83%BD%E9%80%89%E4%B9%A6_a7962778.pdf']
