# After-action: Channel3 deepening, Phase 1 (price history)

## Goal

When a request is far above the product's usual price, it waits for a human.
A seller can raise a listing to match what a fooled agent asks for. Leash's
live-price check (amount ≤ live price + 5%) would then pass, because the
agent's price and the live price agree. The product's own recent history is
the independent answer to "is this price normal?".

## What was built

- **`catalog.price_history()`** calls Channel3
  `GET /v0/price-tracking/history/{id}?days=30` and returns the **median** of
  the daily USD prices with the number of points.
  - **Why the median:** Channel3's `statistics.mean` moves with one odd day,
    and its `std_dev` looked unreliable. In a real sample it was 15.6 for a
    price that never changed.
  - **Which points count:** other currencies and junk points are skipped.
  - **Failures:** it never raises; a failure is `unavailable`.
- **`lookup_product()`** adds the history under `catalog.history`, which costs
  one extra call per payment request. If the history call fails, the lookup
  still succeeds.
- **The `price_spike` rule** in `policy.py`: if the amount is more than
  `price_spike_pct`% (default 25) above the 30-day median, the request is
  **held for a human**, not blocked, because price rises are often real.
  - The rule only runs with at least `price_history_min_points` daily prices
    (default 7).
  - No history, too little history, or a failed history call means the rule
    stays quiet. The live-price and allowlist rules still apply.
  - Both settings are in `config/policy.json`.
- **Explanation:** for example, "$55.00 is more than 25% above this product's
  usual price ($34.77, the median of the last 30 days on Channel3)".
- **Dashboard:** the catalog line adds "(usually $34.77, 30-day median)" and
  turns red on a spike.
- **Docs:** `Catalog.history` and `price_spike` are added to `docs/openapi.yaml`.

`policy.py` is still deterministic: it only reads the history that the
catalog lookup already fetched.

## Real Channel3 run (2026-11-11)

- **What history looks like:** about one price a day per product, for up to
  30 days. Some products have none; for example, 3 of 5 birthday gift
  baskets had 0 points.
- **Spikes found:** 63 real products across 8 searches had a live price. In
  2 of them the cheapest in-stock offer was more than 25% above the 30-day
  median:

  | Product | Live | 30-day median | Days |
  |---|---|---|---|
  | DUPLO Classic Deluxe Brick Box (`YXYmYGf`) | $55.00 | $34.77 | 10 |
  | KitchenAid Classic 4.5 qt mixer (`qW2Da5V`) | $399.95 | $248.00 | 30 |

- **Gate run:** the DUPLO set through the real gate (dry run) gave
  `hold_for_human` with reasons `approval_threshold, price_spike` and the
  explanation above. The dashboard showed it on the dry-run server (checked in
  the browser, no console errors).
- **Latency:** lookup plus history took a median of 0.59 s and at most 0.71 s
  per product.

## Tests

`tests/test_price_history.py` (17 cases):
- The median uses the real response shape.
- Other currencies and junk points are skipped.
- No history means no median.
- A 5xx, bad JSON or 404 makes the history `unavailable`.
- A bad product ID makes no network call.
- The lookup survives a failed history call.
- The rule holds at more than 25%, allows up to 25%, and takes the
  percentage from the policy.
- No history, few points, `unavailable` or a null median keeps the rule quiet.
- A hard rule still blocks alongside a spike.
- End to end through the gate: the request is held, nothing is paid, and the
  explanation is right.

One existing lookup test now passes `with_history=False`, so it checks only
what it checked before. Full suite: **291 passed, 1 skipped**.

## Known limits

- The history is Channel3's price for the product, while the live price is
  the cheapest in-stock offer. When the usual cheap offer goes out of stock,
  that also counts as a spike. That is intended: it's what the buyer would
  pay now compared with normal.
- `/v0/price-tracking` is a v0 path. Channel3 has deprecated its tracking
  subscriptions but says history is still supported.

## Next phases (not started)

- **Phase 2:** price webhooks (Product Subscriptions) for products with a held
  request, marking the hold when the price moves. Needs the Render URL and a
  webhook endpoint registered in the Channel3 dashboard.
- **Phase 3:** show the cheapest approved-seller offer next to each held
  request.
