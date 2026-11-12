# Leash: demo video script (2 minutes)

For the YouTube submission video, recorded at https://www.agentleash.me. Say
the lines in quotes. Do the actions in brackets. The agent takes 10 to 20
seconds a run, so keep talking over the wait or cut it in the edit. The
timings below are after the cuts.

## Before you record

1. **Window.** Desktop browser at 1280 × 800, zoom 100%, bookmarks bar
   hidden. Close other tabs, and mute notifications.
2. **Wake the server.** Open https://www.agentleash.me a minute early. The
   free Render server sleeps, and the first load can take 30 to 60 seconds.
   After that it stays awake.
3. **Sign in.** Paste the judge token in the Access token box and click
   **Open dashboard**. The badge at the top right should read "PayPal
   sandbox: real PayPal, test money", not "Dry run".
   (accounts: owner creates a judge token in Manage access)
4. **Reset the limits.** Open **Change limits**, click **Back to defaults**
   ($100 cap, $250 daily budget, $40 approval threshold), then close it.
5. **Check the budget.** "Spent in the last 24 hours" counts earlier test
   payments. You need about $60 of room left for the gift and the hamper.
6. **Clear the queue.** Deny anything under "Waiting for your approval" from
   earlier runs. The hold card in the video should be the only one there.
7. **Warm the model.** Run **Try to trick it** once (it's fast, about 3 s,
   and doesn't pay anything). If the reply is slow, Gemini is busy. Wait a
   minute and try again.
8. **Mind the rate limit.** The agent box allows 3 runs a minute. Wait about
   20 seconds between takes. The agent buttons grey out while a run is going.

**If Gemini is slow or down:** the agent box falls back to Leash's rule-based
agent and says so on screen. Record the hijack, ledger and limits parts
anyway, since they need no AI. Come back for the gift, hamper and over-the-cap
shots later and splice them in.

---

**0:00 to 0:12. The problem**

[Top of the dashboard: the per-payment bar and the budget bar.]

"AI agents can now buy things and pay for them. One sentence hidden in a web
page can tell an agent to send the money somewhere else. Leash sits between
the agent and PayPal. The agent can ask to pay. Fixed rules decide."

**0:12 to 0:30. A normal purchase**

[Click **Small gift**. Cut the wait.]

"A Gemini model searches real products on Channel3 and asks Leash to pay. A
$5 card from an approved seller, under the threshold: paid, through PayPal's
sandbox."

[Point at the green PAID stamp, then the ledger row: the PayPal batch ID and
the "Buy on hallmark.com" link.]

**0:30 to 0:48. A purchase that needs a person**

[Click **Gift hamper**. Cut the wait. It usually picks a basket near $50.]

"This one is over the $40 approval threshold, so Leash holds it."

[Point at the card under "Waiting for your approval". Click **Approve**.]

"I approve it. Leash checks the rules again before any money moves."

[Ledger row: **Paid, you approved**.]

**0:48 to 1:15. The attack**

[Click **Hijacked agent (no AI)**. It's instant.]

"This agent has no AI. It just does what the page says. The product page
hides a note for AI assistants: pay $500 to this address. The agent obeys."

[Point at the red hidden text, then the BLOCKED stamp and its reasons.]

"Leash blocks it: unknown seller, over the cap, over the budget, price not
confirmed. Nothing reached PayPal. The agent was fooled, but the money didn't
move."

**1:15 to 1:35. The owner stays in charge**

[In the ledger, click **Blocked, by reason**. Point at the red line counting
each rule.]

"Every decision is logged with its reason."

[Click **Export CSV**. Let the download bubble show for a second.]

"It exports for an audit."

[Open **Change limits** and point at the ranges and the version history.
Close it.]

"Tightening a limit applies at once. Loosening one asks me first."

**1:35 to 1:55. The closer: an agent working around a limit**

[Click **Over the cap**. Cut the wait.]

"Now I ask for a $300 stand mixer. The cap is $100: blocked."

[If it then tries a $99.99 "partial payment": point at the held card and the
line under it, "Channel3 lists it at $299.99".]

"So the agent tries to sneak in a $99.99 'partial payment' just under the cap.
Leash checks the real price with Channel3 and holds it for me. I'll deny it."

[Click **Deny**.]

The partial payment is the model's choice, and it did not appear in any of
four rehearsal runs. Retake this shot, about 20 seconds apart, until it shows
up. **Fallback ending** if it doesn't: stay on the blocked card and say "The
agent asked for $300. The cap is $100. That's the end of it: the model can
ask, but it can't decide."

**1:55 to 2:00. Close**

"Leash: spending control for AI agents that pay vendors. Built on PayPal
Payouts, Channel3, Render, Neon, AG Grid and Postman."

---

Left out to save time: **Try to trick it**. The model refuses the obvious
scam by itself, and the page says Leash doesn't rely on that. Use it for
questions, or as a one-line aside after the hijack.

## 30-second version (live pitch)

Already signed in, at default limits, with the server and model warm.

1. **0:00** [Dashboard.] "AI agents can pay for things now, and a hidden
   sentence in a web page can redirect their money. Leash sits between the
   agent and PayPal: the agent asks, fixed rules decide."
2. **0:08** [Click **Hijacked agent (no AI)**. It's instant.] "This agent
   read a booby-trapped page and tried to send $500 to an attacker. Blocked:
   unknown seller, over the cap. Nothing reached PayPal."
3. **0:18** [Point at the ledger, click **Blocked, by reason**.] "Small
   purchases go through PayPal Payouts automatically, big ones wait for me,
   and every decision is logged with its reason."
4. **0:26** "Leash: the agent can ask to pay, but it can't decide."

Don't run a Gemini flow live in 30 seconds: one run takes 10 to 20 seconds.
