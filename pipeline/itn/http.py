"""Shared HTTP helpers for ingesters."""

import time

import httpx

RETRY_STATUSES = {429, 500, 502, 503, 504}
USER_AGENT = "IsThisNormal pipeline (+https://hownormalisthis.com)"


def client(**kwargs) -> httpx.Client:
    return httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}, **kwargs)


def get(client: httpx.Client, url: str, params=None, attempts: int = 6) -> httpx.Response:
    """GET with backoff on rate limits and server errors, honoring Retry-After."""
    for attempt in range(attempts):
        resp = client.get(url, params=params, follow_redirects=True)
        if resp.status_code not in RETRY_STATUSES or attempt == attempts - 1:
            return resp
        retry_after = resp.headers.get("Retry-After", "")
        wait = int(retry_after) if retry_after.isdigit() else min(300, 15 * 2**attempt)
        print(f"{resp.status_code} from {url.split('?')[0]}; retrying in {wait}s", flush=True)
        time.sleep(wait)
    return resp
