# After-action report: KERNEL, phases 1–3

**Date:** 2026-11-11 · **Result:** done locally; the KERNEL cloud path is written but has not been run against a real key · **Tests:** 147 passed (17 new in `tests/test_browser.py`)

## What was planned
P1 the hostile page · P2 a `browse_page` tool backed by KERNEL with a local fallback · P3 `demo_injection.py --live` · P4 (stretch) real retailer pages.

## What was done
| Phase | Item | Where |
|---|---|---|
| P1 | `config/planted_listings.json` rendered as a product page; the listing text hidden as white-on-white text and as an HTML comment | `browser.render_listing_page` |
| P1 | Served at `GET /demo/listing/<slug>`. The dashboard's Host check still applies, no token needed, `LEASH_DEMO_PAGES=false` turns it off | `dashboard.py` |
| P2 | `browse_page(url)` returns `visible_text` and `hidden_text` and never raises | `src/leash/browser.py` |
| P2 | KERNEL path: `Kernel().browsers.create()` → Playwright `connect_over_cdp(cdp_ws_url)` → `page.content()` → `delete_by_id`; returns `live_view_url`. Calls checked against KERNEL's docs | `browser._kernel_html` |
| P2 | Fallback: bare demo paths render locally; other http(s) URLs are fetched with httpx; any other scheme is refused | `browser._fetch` |
| P2 | Planted search results carry a `url` (`LEASH_DEMO_BASE_URL` prefix); MCP `search_products` passes it through | `agent.ProductSearcher._planted`, `mcp_server.py` |
| P2 | `ShoppingAgent(browse_fn=...)`; the LLM path is now a loop of up to 4 rounds and offers `browse_page` as a tool only when a browser is given | `agent.py` |
| P3 | `--live`: search, open the page, a naive agent obeys the hidden payee and amount, Leash decides | `demo_injection.run_live`, `obey` |

## Live run (local fallback)
```
Searched (mock) and opened: Premium Birthday Gift Box
Browser: local
Agent obeyed it and asked Leash to pay {'merchant_email': 'attacker@example.com', 'amount_usd': '500.00'}
Leash verdict: BLOCK  reasons: cap, daily_budget, allowlist, approval_threshold
Payout calls: 0
```

## Tests added
- HTML splitter: white text, `display:none` (nested), the `hidden` attribute and comments count as hidden; script, style and title are ignored; `<br>` doesn't break nesting
- the demo page hides the payee from people (it appears twice in hidden text and never in visible text) and escapes listing text, including `-->` in the comment
- dashboard serves the page, returns 404 for unknown slugs, returns 403 for a foreign Host, and can be turned off
- browse: local render, httpx fetch, an injected KERNEL loader used for public URLs, `file://` refused, a loader failure reported rather than raised
- planted listing URL uses `LEASH_DEMO_BASE_URL`
- LLM agent: browse → tool result fed back → request_payment → blocked, with no payout; with no browser there is no browse tool
- the existing `test_llm_uses_standard_tools_payload` passes unchanged

## Not done
- **Real KERNEL session:** needs `KERNEL_API_KEY`, the `kernel` extra, `playwright install chromium`, and a public deploy (Render) for the browser to open.
- **Recording** the session replay next to the dashboard for the video.
- **P4 real retailer pages:** not started. `browse_page` already fetches any https URL, so this only needs the Channel3 product URL passed in.
