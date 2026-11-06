"""Check the actual public PDF before exposing a successful report result."""
import asyncio
import urllib.parse
import urllib.request


def request_url(url: str) -> str:
    """The URL as it goes on the wire. Report file names carry the paper's
    title, so a Chinese or no-DOI paper yields a path with non-ASCII
    characters; http.client encodes the request line as ASCII and fails on
    them. Percent-encode the path and query (existing escapes stay as they
    are); the stored URL itself is unchanged."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=~-._")
    query = urllib.parse.quote(parts.query, safe="=&%:@!$'()*+,;/?~-._")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def _check(url: str) -> None:
    request = urllib.request.Request(request_url(url), headers={'Range': 'bytes=0-4'})
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status not in (200, 206) or response.read(5) != b'%PDF-':
            raise RuntimeError(f'Public report is not an accessible PDF: {url}')


async def verify_public_pdf(url: str) -> None:
    if not url:
        return
    for attempt in range(2):
        try:
            await asyncio.to_thread(_check, url)
            return
        except (OSError, RuntimeError):
            if attempt:
                raise
            await asyncio.sleep(2)
