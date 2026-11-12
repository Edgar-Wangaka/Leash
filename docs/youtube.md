# YouTube upload: Leash demo video

**File:** `leash-demo.mp4` (1920 × 1080, H.264 + AAC, 1:57)

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

Try it: https://www.agentleash.me
Code: https://github.com/Edgar-Wangaka/Leash
Postman workspace (API and attack tests): https://app.getpostman.com/run-collection/59387021-212e4c48-f29a-4b46-9b4d-3151e416cdc9?action=collection%2Ffork&source=rip_markdown&collection-url=entityId%3D59387021-212e4c48-f29a-4b46-9b4d-3151e416cdc9%26entityType%3Dcollection%26workspaceId%3D8ebb3b4e-6964-4ee0-aba7-3a290a44eaa8

Built with PayPal Payouts, Channel3, Gemini, Render, Neon, AG Grid and Postman.

Chapters
0:00 The problem: hidden text can redirect an agent's money
0:13 A small purchase, paid through PayPal Payouts
0:28 Over the approval threshold: held, then approved
0:50 A hijacked agent tries to send $500: blocked
1:10 The ledger: reasons, CSV export, limits
1:33 Over the per-payment cap: blocked
1:46 Leash

---

## Upload checklist

1. YouTube Studio → **Create** → **Upload videos** → pick `leash-demo.mp4`.
2. Paste the title and description above.
3. Thumbnail: let YouTube pick one, or upload a screenshot of the BLOCKED card.
4. **Audience:** "No, it's not made for kids".
5. **Visibility:** Unlisted → **Save**. Copy the link into the submission form.
6. Check that chapters appear under the player (the first chapter must start at 0:00).
