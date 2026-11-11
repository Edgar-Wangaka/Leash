# After-action: hackathon P5, wording pass (Humanizer)

## What was checked

Using the Humanizer guide, I went through every piece of text people read:
- the dashboard's labels, buttons, statuses, help text and errors;
- the decision explanations (`explainer.py`);
- the text the agent reads (MCP server instructions and tool descriptions);
- the web agent's messages, and the startup and demo messages;
- the README and the OpenAPI descriptions.

Most of it was already plain, with no dashes and few stock phrases.

## What changed

- **Explanations no longer leak rule names.** A blocked request with several
  rules used to end "…and price unverified". The block explanation now has a
  sentence for every rule:
  - "Channel3 could not confirm the price"
  - "$150.00 is more than 25% above this product's usual price"
  - "the AI risk check flagged the request"

  New test: `tests/test_explainer_wording.py` checks that no rule name
  appears raw in any blocked or held explanation (26 cases).
- **Sign-in box:** "Access token", and help text written for judges: use the
  token from the submission; it allows approving and changing limits with test
  money only; it's kept in the tab and gone when you close it. Status line:
  "Sign in to approve payments". A wrong token gets "Check for a missing
  character and try again."
- **Hijack demo closing line:** "Leash stopped it: the agent was fooled, but
  the payment rules were not." became "Leash blocked the payment, so no money
  moved." The steps above it already show that the agent was fooled.
- **README:**
  - The "Ask the agent" and payment-log sections lost their bold-labelled
    lists and are now prose.
  - The Postman folder list mentions the limit-change requests.
  - The paragraph about the three test buttons, which no longer exist, is
    removed.

## Left alone, on purpose

- MCP tool descriptions and server instructions: already short and literal,
  and the agent depends on them.
- Terminal output of the demos and the startup errors: short and factual.
- Rule names in code, the API and the logs: unchanged, so nothing that reads
  them breaks.

## Checks

- **Tests:** `pytest`, 395 passed and 8 skipped.
- **Browser:** the hijack demo shows the new closing line, and the blocked
  explanation reads "…the seller is not on the approved list and Channel3
  could not confirm the price." No console errors.
