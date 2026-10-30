"""Check the actual public PDF before exposing a successful report result."""
import asyncio
import urllib.request


def _check(url: str) -> None:
    request = urllib.request.Request(url, headers={'Range': 'bytes=0-4'})
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
