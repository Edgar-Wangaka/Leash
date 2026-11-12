"""The dashboard page loads AG Grid safely and never builds HTML from request text."""

from __future__ import annotations

import re
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "src/leash/dashboard.html").read_text()


def test_ag_grid_is_pinned_and_checked_by_hash():
    tags = re.findall(r"<script[^>]*src=\"([^\"]+)\"[^>]*>", PAGE)
    assert tags == ["https://cdn.jsdelivr.net/npm/ag-grid-community@36.2.0/dist/ag-grid-community.min.js"]
    tag = re.search(r"<script[^>]*ag-grid-community@36\.2\.0[^>]*>", PAGE).group(0)
    assert 'integrity="sha384-' in tag and 'crossorigin="anonymous"' in tag


def test_no_html_is_built_from_strings():
    # Request text comes from agents and sellers. Cells use textContent only.
    script = PAGE[PAGE.index("<script>"):]
    assert "innerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert "outerHTML" not in script


def test_the_ledger_has_search_filters_export_and_a_fallback():
    for marker in ('id="ledgerSearch"', 'data-outcome="blocked"', 'id="byReason"',
                   'id="exportCsv"', 'id="ledgerFallback"', "exportDataAsCsv", "getRowId"):
        assert marker in PAGE, marker
    # If the CDN is unreachable, the page still lists payments.
    assert "if (!grid) return renderFallback(rows);" in PAGE


def test_buy_links_open_safely():
    assert 'link.rel = "noopener noreferrer"' in PAGE
    assert "/^https?:\\/\\//i.test(url)" in PAGE
