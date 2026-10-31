"""Shared HTTP helpers for ingesters."""

import time

import httpx

# 520-524 are Cloudflare's origin errors, which Congress.gov returns intermittently.
RETRY_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
USER_AGENT = "IsThisNormal pipeline (+https://hownormalisthis.com)"


def client(timeout: float = 120, **kwargs) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, **kwargs)


def get(client: httpx.Client, url: str, params=None, attempts: int = 6,
        max_wait: float | None = None) -> httpx.Response:
    """GET with backoff on rate limits, server errors and dropped connections,
    honoring Retry-After. A wait longer than max_wait returns the error response instead."""
    for attempt in range(attempts):
        try:
            resp = client.get(url, params=params, follow_redirects=True)
        except httpx.TransportError as err:
            if attempt == attempts - 1:
                raise
            wait = min(300, 15 * 2**attempt)
            print(f"{type(err).__name__} from {url.split('?')[0]}; retrying in {wait}s", flush=True)
            time.sleep(wait)
            continue
        if resp.status_code not in RETRY_STATUSES or attempt == attempts - 1:
            return resp
        retry_after = resp.headers.get("Retry-After", "")
        wait = int(retry_after) if retry_after.isdigit() else min(300, 15 * 2**attempt)
        if max_wait is not None and wait > max_wait:
            return resp
        print(f"{resp.status_code} from {url.split('?')[0]}; retrying in {wait}s", flush=True)
        time.sleep(wait)
    return resp
