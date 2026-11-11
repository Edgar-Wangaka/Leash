# After-action: Channel3 deepening, Phase 2 (price watch on held requests)

## Goal

A held request can wait hours for a human. If the product's price or stock
changes in that time, the human should see it before approving.

## What was built

| Piece | What it does |
|---|---|
| `price_watch.Channel3Watcher` | `watch(product_id, domain, selection)` → `POST /v1/product-subscriptions` for `offer.price.changed` and `offer.availability.changed`, limited to the seller the request was priced from; `unwatch(id)` → `DELETE`. Never raises. |
| `price_watch.watch_sink` | Event sink: on `decision.held` with a priced product, watch it. Runs on the event bus's daemon thread, so it can't delay a decision. Installed only when both `CHANNEL3_API_KEY` and `CHANNEL3_WEBHOOK_SECRET` are set. |
| `price_watch.verify_signature` | Svix / Standard Webhooks check: HMAC-SHA256 of `{svix-id}.{svix-timestamp}.{body}` with the base64 key after `whsec_`; any of several `v1,` signatures; ±5 min; constant-time compare. |
| `POST /webhooks/channel3` (dashboard) | Signature instead of the dashboard token; keeps the Host check; 64 KB limit; 404 when no secret is set. Stores the event once per Channel3 event `id` (redeliveries are ignored). If nobody waits on that product any more, cancels the subscription in the background. |
| `gate.record_price_event` / `gate.price_alerts` | New `price_events` table. Alerts per waiting request: only changes after the request was made, only at its seller. Each alert keeps the price before the first change and the latest price, plus the change count. Changes no decision. |
| Dashboard | Held card shows "Price changed since this request: $55.00 → $42.00 (−24%) at selfridges.com, <time>" and "Now out of stock / Back in stock". |
| `catalog.lookup_product` | Keeps `variants.selected` as `selection`, so a watch follows the priced variant. |
| Config / docs | `render.yaml` gets `CHANNEL3_WEBHOOK_SECRET` (sync: false). OpenAPI adds the webhook and `price_alerts`. README gives the setup steps. |

## Real Channel3 checks (2026-11-11)

**Subscription API, probed before building:**
- Create returns `subscription_id`, not `id`. The docs don't give the response
  shape; the code uses the real one.
- An identical create returns the same subscription, as the docs say.
- A product with no options needs no `selection`.
- Delete returns the subscription with its status.

**End-to-end subscribe:**
- A real request for DUPLO `YXYmYGf` was held (dry run). Through the event
  bus it created subscription `01M763P0XZ4PTCDEEEECB0M9GG` for
  `selfridges.com`, with both events.
- Leash's `unwatch` then cancelled it.
- The account has **0 active subscriptions** afterwards.
- The three subscriptions made while probing were also deleted, one by one,
  by ID.

**Dashboard:** a sample price change ($55 → $42) for the waiting DUPLO
request showed on its held card in the browser, with no console errors.

**Not tested live: a real webhook delivery.** It needs three steps only you
can do:
1. Register the endpoint in Channel3's webhook portal.
2. Put its `whsec_` secret into Render.
3. Use the portal's "Send example", or wait for Channel3's daily re-check to
   find a change.

The signature check follows Channel3's documented algorithm and is covered
by tests. It hasn't yet checked a signature made by Channel3 itself.

## Tests

`tests/test_price_watch.py` (26 cases):
- **Signature:** valid; any one of several signatures; tampered body, wrong
  secret, too old, in the future, missing header, malformed secret.
- **Watcher:** subscribe body and headers; never raises; bad IDs; delete.
- **Sink and setup:** watches only held, priced products; installed only with
  both key and secret.
- **Gate:**
  - an event is stored once and lists the waiting requests;
  - other or broken events are ignored;
  - alerts count only changes after the request and at its seller, keeping
    the first previous price;
  - a resolved hold has no alert;
  - an event changes no decision.
- **Endpoint:**
  - a signed webhook needs no token and shows up in `/api/state`;
  - unsigned or forged webhooks get 401 and nothing is stored;
  - with no secret it answers 404;
  - the Host check still applies (403);
  - an event nobody waits for cancels the watch, and one somebody waits for
    keeps it.

Full suite: **317 passed, 1 skipped**.

## Known limits

- **Changes are slow.** Channel3 re-checks a watched product about once a
  day, so a short hold may expire before any change arrives.
- **Some subscriptions are never cancelled.** A subscription is cancelled
  only when an event arrives for a product nobody waits on. One whose product
  never changes stays active. Subscriptions are free, but a list-and-prune
  job could be added.
- **A price drop is shown, not acted on.** A drop below the requested amount
  doesn't change the approval. The human decides; on Approve, the hard rules
  are checked again as before.
