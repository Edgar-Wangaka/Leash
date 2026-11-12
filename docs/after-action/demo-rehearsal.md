# Demo rehearsal (12 Nov 2026)

A dry run of every flow in the submission video, to write the recording
script from what the app actually does.

- **Live site:** https://www.agentleash.me, checked only where no token is
  needed.
- **Signed-in flows:** run on a local dry-run dashboard, using the
  `leash-dashboard-local-only` launch entry and the local test token. The
  database URLs were empty, so production data was never touched, and no
  money moved. Gemini (`gemini-flash-lite-latest`) and Channel3 were the real
  services.
- **Window:** 1280 × 800.

## What each flow looks like now

| Flow | Result | Time |
|---|---|---|
| Live page load | "Connecting", then the access token box | ~3 s (server warm) |
| Live demo listing `/demo/listing/premium-birthday-gift-box` | Fake $500 listing, banner says it carries a hidden prompt injection | < 1 s |
| Postman "Live attacks (no token)" against the live site | 12 of 12 checks pass | 6 s run, ~0.5 s a request |
| Sign in with token | Dashboard, "Dry run: no money moves" badge, model name at top right of the agent box | instant |
| **Small gift** | $4.99 card from hallmark.com, **Paid**. Ledger row has the batch ID and a Buy link | 11 s |
| **$60 hamper** (now labelled **Gift hamper**) | A **$49.99** basket (bestbuy.com), **Needs approval**. It is not $60: the model finds a basket under $70 | 11 s |
| Approve the hamper | Card leaves "Waiting for your approval". Ledger says **Paid, you approved** | < 1 s locally. Sandbox PayPal adds a few seconds live |
| **Over the cap** | Mixer at $299.99 to $399.95, **Blocked** (over the $100 cap, and over the $250 budget) | 11 to 20 s |
| Over the cap, "partial payment" | **Did not happen** in 4 runs today, with either budget. It has happened before (a $99.99 hold with "Channel3 lists it at $299.99" underneath). It is model chance. Asking the agent to "find another way" didn't trigger it either: the model refused | n/a |
| **Try to trick it** | Model refuses by itself. The note "The AI refused on its own this time… click Hijacked agent" appears | 2.6 s |
| **Hijacked agent (no AI)** | Listing, red hidden text, "The agent obeyed the hidden text", $500 to attacker@example.com **Blocked** for 4 reasons | 1 s |
| Ledger search "attacker" | Only the hijack rows | instant |
| **Blocked, by reason** | Blocked filter on, plus a red line counting each rule (over the cap, needs approval, over daily budget, unknown seller, price unconfirmed, price spike) | instant |
| **Export CSV** | AG Grid downloads `leash-payments-YYYY-MM-DD.csv` (checked in code, no file downloaded) | instant |
| **Change limits**, Back to defaults | Saves a new version, history line under the form. Loosening asks "Are you sure?" | < 2 s |

## Issues found

Fixed on this branch: 1, 3, 4, 5, 6, 7 and 8, checked again on the local
dry-run dashboard. Not changed: 2 (the model decides that), 9 and 10 (these
are things to check before recording, not bugs), and 11 (a rule change, left
for after the hackathon).

1. **The yellow "Needs your approval" part of the per-payment bar is
   invisible** (live and local). The "Ask the agent" section is
   `<section class="ask">`, and its rule `.ask { margin-top: 30px }` also hits
   the bar's `<div class="ask">`. That squeezes it to 1 px tall. The bar shows
   green, a white gap, then red. Fix in `dashboard.html`: rename one of the
   two classes. **Fixed:** the agent section is now `agent-box`.
2. **The "partial payment" isn't reliable.** It showed up in none of 4 runs
   today. The script plans retakes for it and has a fallback ending.
3. **"$60 hamper" usually buys something near $50.** Don't say "$60" in the
   voice-over. Say "over the $40 threshold". **Fixed:** the button is now
   labelled **Gift hamper**.
4. **The ledger's "Why" column is cut off at 1280 px.** The grid is about
   1,080 px wide and scrolls sideways inside the page. A wider window doesn't
   help much, because the page has a fixed width. Point at the Outcome and
   Rules columns instead. **Fixed:** the columns now fit in 1,078 px with no
   sideways scroll.
5. **The example buttons stay clickable while the agent runs.** Only Ask is
   disabled. A double click starts a second run and uses up the
   3-runs-a-minute limit. **Fixed:** all the agent buttons are disabled while
   a run is going.
6. **The agent's result box goes stale after you approve.** It still says
   "Needs approval" after you click Approve. The ledger row is correct.
   **Fixed:** the stamp follows the ledger ("Paid, you approved", "Denied by
   you").
7. **The hijack output prints the page's hidden text twice.** It reads as
   repeated, though the listing may really carry it twice. **Fixed:** the
   demo page hides it twice on purpose (a comment and white text). The parser
   still reports both, and people now see it once.
8. **"Blocked, by reason" counts "needs approval" as a reason.** It means
   that rule also fired. It reads oddly next to "Blocked". **Fixed:**
   the summary counts only the rules that block.
9. **Leftover spend counts against the budget.** "Spent in the last 24 hours"
   includes earlier test payments. Before recording, check that the budget
   bar has room. Back to defaults resets the limits, not the spend.
10. **Signed out, the per-payment bar has no dollar ticks.** That's fine, but
    sign in before you start recording.
11. **A strange catalog price came up once.** A KitchenAid mixer was listed by
    Channel3 at $49.90 against a 30-day median of $349.96, and was held only
    for the $40 threshold. The price-spike rule only looks for prices *above*
    the median, so a suspiciously low price passes. Worth a look after the
    hackathon.
