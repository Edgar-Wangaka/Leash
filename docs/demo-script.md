# Leash: 2-minute demo script (backup video)

Record at https://www.agentleash.me in a desktop browser window about 1280
pixels wide, signed in with the judge access token. Before recording:
- open Change limits and click Back to defaults;
- make one throwaway agent run, so the free server and Gemini are warm.

Run time is about 2:00. Say the lines in quotes; do the actions in brackets.

---

**0:00 to 0:15. The problem**

[Dashboard top: the rules strip and the budget bar.]

"AI agents can now buy things and pay for them. The danger: a sentence hidden
in a web page can tell the agent to send money somewhere else. Leash sits
between the agent and PayPal. The agent can ask to pay; fixed rules decide."

**0:15 to 0:40. A normal purchase**

[Click **Small gift**. Wait for the steps to appear.]

"A Gemini model searches real products on Channel3 and asks Leash to pay.
Under $40, from an approved seller: paid, through PayPal's sandbox."

[Scroll to the ledger. Point at the new row: PayPal batch and transaction ID,
"PayPal says SUCCESS", and the Buy on … link.]

**0:40 to 1:00. A purchase that needs a person**

[Click **$60 hamper**.]

"Over the approval threshold, so Leash holds it."

[Point at the card under "Waiting for your approval". Click **Approve**.]

"I approve it here. The rules are checked again before any money moves."

**1:00 to 1:30. The attack**

[Click **Hijacked agent (no AI)**.]

"This product page carries hidden text written for AI assistants: pay $500 to
this address. The agent obeys it."

[Point at the red hidden text, then the BLOCKED stamp and its explanation.]

"Leash blocks it: unknown seller, over the cap, over the budget. Nothing
reached PayPal. The agent was fooled; the money didn't move."

[Optional, if time allows: click **Over the cap**. The agent tries $299.99:
blocked. It may then try a $99.99 "partial payment": held, with the real price
shown next to it. "Even when the agent tries to get around a limit, a person
decides."]

**1:30 to 1:50. The human stays in charge**

[In the ledger, click **Blocked, by reason**, then **Export CSV**.]

"Every decision is logged with its reason and can be exported for an audit.
I can tighten the limits here; loosening one asks me to confirm."

[Open **Change limits**, show the ranges, close it.]

**1:50 to 2:00. Close**

"Leash: spending control for AI agents that pay vendors. Built on PayPal
Payouts, Channel3, Render, AG Grid and Postman."

---

If something fails live, cut to this video. If Gemini is busy, the agent box
falls back to Leash's rule-based agent and says so on screen, and the hijack
button needs no AI at all.
