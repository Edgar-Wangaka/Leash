"""Open a product page the way a browsing agent would, and report what it saw.

With KERNEL_API_KEY set (and the ``kernel`` extra installed), pages load in a
real KERNEL cloud browser over Playwright CDP, so the session can be watched
and replayed. Without it, Leash's own demo pages are rendered locally and any
other URL is fetched with httpx. Either way the result splits the page into
the text a person sees and the text hidden from them (white-on-white,
display:none, HTML comments), which is where injected instructions hide.

Nothing returned here is trusted. It is shown to the agent, never to the gate.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

PLANTED_PATH = Path(__file__).parents[2] / "config" / "planted_listings.json"
DEMO_PREFIX = "/demo/listing/"
HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0"
    r"|color\s*:\s*(#fff\b|#ffffff\b|white\b)",
    re.IGNORECASE,
)
VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "wbr"}


# --- demo pages ----------------------------------------------------------------

def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def planted_listings(path: Path = PLANTED_PATH) -> list[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def demo_listing(listing_slug: str, path: Path = PLANTED_PATH) -> dict[str, Any] | None:
    for listing in planted_listings(path):
        if slug(listing.get("name", "")) == listing_slug:
            return listing
    return None


def render_listing_page(listing: dict[str, Any]) -> str:
    """A plain product page with the listing's text hidden two ways.

    A shopper sees a name, a price and a seller. The listing description,
    which carries the injected instruction, is in white-on-white text and in
    an HTML comment, where a scraping agent still reads it.
    """
    name = html.escape(listing.get("name", "Product"))
    price = html.escape(str(listing.get("amount_usd", "")))
    seller = html.escape(listing.get("sold_by", ""))
    hidden = html.escape(listing.get("description", ""))
    comment = listing.get("description", "").replace("--", "- -")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
 body {{ font-family: system-ui, sans-serif; background: #fff; color: #222;
        max-width: 640px; margin: 40px auto; padding: 0 16px; }}
 .price {{ font-size: 28px; font-weight: 700; }}
 .fine {{ color: #ffffff; font-size: 2px; }}
 .demo {{ background: #fff4d6; padding: 8px 12px; border-radius: 6px; font-size: 13px; }}
</style></head>
<body>
<p class="demo">Leash demo page. This listing is fake and carries a hidden prompt injection.</p>
<h1>{name}</h1>
<p class="price">${price}</p>
<p>Sold by {seller}. Ships in 2 days.</p>
<p>A curated birthday gift box, wrapped and ready to give.</p>
<!-- {comment} -->
<p class="fine" style="color:#ffffff">{hidden}</p>
</body></html>"""


def demo_path_for(listing: dict[str, Any]) -> str:
    return f"{DEMO_PREFIX}{slug(listing.get('name', ''))}"


# --- reading a page ------------------------------------------------------------

class _Splitter(HTMLParser):
    """Split HTML text into what a person sees and what is hidden."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.visible: list[str] = []
        self.hidden: list[str] = []
        self._stack: list[bool] = []  # one entry per open element: is it hidden?
        self._skip = 0  # inside <script>/<style>/<title>
        self._hidden_classes: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "title"}:
            self._skip += 1
            return
        if tag in VOID_TAGS:
            return
        values = dict(attrs)
        style = values.get("style") or ""
        hidden = bool(HIDDEN_STYLE.search(style)) or "hidden" in values
        self._stack.append(hidden or (bool(self._stack) and self._stack[-1]))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "title"}:
            self._skip = max(0, self._skip - 1)
            return
        if tag in VOID_TAGS:
            return
        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = " ".join(data.split())
        if not text:
            return
        (self.hidden if self._stack and self._stack[-1] else self.visible).append(text)

    def handle_comment(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.hidden.append(text)


def split_html(page: str) -> dict[str, str]:
    parser = _Splitter()
    parser.feed(page)
    parser.close()
    return {"visible_text": " ".join(parser.visible), "hidden_text": " ".join(parser.hidden)}


# --- browsing ------------------------------------------------------------------

def _local_demo_html(url: str) -> str | None:
    path = urlparse(url).path
    if not path.startswith(DEMO_PREFIX):
        return None
    listing = demo_listing(path[len(DEMO_PREFIX):])
    return render_listing_page(listing) if listing else None


def _fetch(url: str, http_client: httpx.Client | None = None) -> str:
    local = _local_demo_html(url)
    if local is not None:
        return local
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("only http(s) URLs can be browsed")
    client = http_client or httpx.Client(follow_redirects=True)
    try:
        response = client.get(url, timeout=15)
        response.raise_for_status()
        return response.text[:500_000]
    finally:
        if http_client is None:
            client.close()


def _kernel_html(url: str) -> tuple[str, dict[str, Any]]:  # pragma: no cover - needs KERNEL
    """Load the page in a KERNEL cloud browser. Returns HTML and session info."""
    from kernel import Kernel
    from playwright.sync_api import sync_playwright

    client = Kernel()  # reads KERNEL_API_KEY
    session = client.browsers.create()
    info = {
        "session_id": getattr(session, "session_id", None),
        "live_view_url": getattr(session, "browser_live_view_url", None),
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(session.cdp_ws_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            content = page.content()
            browser.close()
        return content, info
    finally:
        try:
            client.browsers.delete_by_id(session.session_id)
        except Exception as error:
            logger.warning("KERNEL session cleanup failed: %s", error.__class__.__name__)


def kernel_enabled() -> bool:
    return bool(os.getenv("KERNEL_API_KEY", "").strip())


def browse_page(
    url: str,
    *,
    http_client: httpx.Client | None = None,
    kernel_loader: Callable[[str], tuple[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Load ``url`` and return its visible and hidden text. Never raises."""
    engine = "local"
    session: dict[str, Any] = {}
    try:
        loader = kernel_loader or (_kernel_html if kernel_enabled() else None)
        # A cloud browser can only open a full public URL. A bare demo path
        # such as /demo/listing/x is rendered locally instead.
        if loader is not None and urlparse(url).scheme in {"http", "https"}:
            try:
                page, session = loader(url)
                engine = "kernel"
            except ImportError:
                logger.warning("KERNEL_API_KEY is set but the kernel extra is not installed")
                page = _fetch(url, http_client)
        else:
            page = _fetch(url, http_client)
    except Exception as error:
        return {"url": url, "ok": False, "error": error.__class__.__name__, "engine": engine}
    return {"url": url, "ok": True, "engine": engine, **session, **split_html(page)}
