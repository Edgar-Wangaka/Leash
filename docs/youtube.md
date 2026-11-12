# YouTube upload: Leash demo video

**File:** `leash-demo-final.mp4`, your iMovie export of `leash-demo-3min.mp4` with your voice (1920 × 1080, 2:57). Fallback: `leash-demo-v1.mp4` (1:57, computer voice).

**Visibility:** Unlisted (anyone with the link can watch; it won't show up in
search). Switch to Public only if the hackathon rules ask for it.

**Category:** Science & Technology. **Made for kids:** No.

## Title

Leash: spending control for AI agents that pay with PayPal

## Description

Paste everything between the lines.

---

AI agents can now buy things and pay for them. One sentence hidden in a web
page can tell an agent to send the money somewhere else. Leash sits between
the agent and PayPal: the agent can ask to pay, and fixed rules decide.

In this demo (PayPal sandbox, test money only):
- A Gemini agent finds a $4.99 card on Channel3, and Leash pays it through PayPal Payouts.
- A gift hamper over the $40 approval threshold is held until a person approves it.
- A hijacked agent obeys hidden text on a product page and tries to send $500. Leash blocks it, and no money moves.
- Every decision is logged with its reason, exports to CSV, and limits change from the dashboard.
- An agent tries to buy a $350 stand mixer, over the $100 cap: blocked.

Narrated as a short story: Maya, an events planner, hands her shopping to an AI agent and keeps it on a leash.

Try it: https://www.agentleash.me
Code: https://github.com/Edgar-Wangaka/Leash
Postman workspace (API and attack tests): https://app.getpostman.com/run-collection/59387021-212e4c48-f29a-4b46-9b4d-3151e416cdc9?action=collection%2Ffork&source=rip_markdown&collection-url=entityId%3D59387021-212e4c48-f29a-4b46-9b4d-3151e416cdc9%26entityType%3Dcollection%26workspaceId%3D8ebb3b4e-6964-4ee0-aba7-3a290a44eaa8

How it's built:
- PayPal Payouts moves the money (sandbox in this demo).
- Channel3 supplies real products and verifies prices.
- Gemini drives the demo agent; any agent can connect over MCP (search, pay, check status).
- Neon Postgres stores the ledger and the limits, with a version history.
- Render hosts the dashboard and the MCP server.
- AG Grid renders the ledger.
- Kernel runs the booby-trapped page in a real cloud browser.
- Postman attacks the API (wrong tokens, cross-site requests) on every push.

Chapters
0:00 Meet Maya
0:18 The deal: the agent asks, the rules decide
0:34 Monday: a $4.99 card, paid through PayPal Payouts
1:01 A bigger ask: held for approval
1:24 The bad day: a hijacked agent tries to send $500
1:47 Friday: the ledger, CSV export and limits
2:10 The mixer: over the cap
2:22 Under the hood: MCP, Gemini, Channel3, PayPal, Neon, Render, Kernel, Postman
2:48 Leash
---

## Upload checklist

1. YouTube Studio → **Create** → **Upload videos** → pick `leash-demo-final.mp4`.
2. Paste the title and description above.
3. Thumbnail: let YouTube pick one, or upload a screenshot of the BLOCKED card.
4. **Audience:** "No, it's not made for kids".
5. **Visibility:** Unlisted → **Save**. Copy the link into the submission form.
6. Check that chapters appear under the player (the first chapter must start at 0:00).
