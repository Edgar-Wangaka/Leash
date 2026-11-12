# After-action: hackathon P6, finish

## What was done

- **Keep-awake ping** (`.github/workflows/keepalive.yml`): every 10 minutes it
  calls `/healthz` on www.agentleash.me and leash-1pqh.onrender.com, so judges
  don't wait about 50 seconds for Render's free plan to wake up. It can also
  be started by hand, and it has no permissions or secrets.
  GitHub runs scheduled workflows only from the default branch, so it starts
  once `sponsor-integrations` is merged into `main`.
- **README "Try it (for judges)" section**, near the top: sign in with the
  access token from the submission, then five things to try (small gift paid,
  $60 hamper held, try to trick it, hijacked agent blocked, change limits).
  The outcome table now also lists the hold reasons added since it was
  written: price unconfirmed, and price spike above the 30-day median.
- **Postman:** the `Leash Render` environment now points at
  `https://www.agentleash.me`.

## Final checks on the live site (2026-11-12, no token used)

| Check | Expected | Got |
|---|---|---|
| Page loads | 200 | 200 |
| State without a token / with a wrong token | 401 | 401 / 401 |
| Approve without a token | 401 | 401 |
| Approve from a cross-site page | 403 | 403 |
| Approve as a form post | 403 | 403 |
| Change limits without a token | 401 | 401 |
| Agent run without a token | 401 | 401 |
| The agent's bearer-token style on approve | 401 | 401 |
| MCP without a bearer token | 401 | 401 |
| Channel3 webhook without a signature | 401 | 401 |
| Demo listing page (public on purpose) | 200 | 200 |
| `agentleash.me` | HTTPS | 301 to `https://www.agentleash.me` (certificate issued) |

Earlier in this run on the live site:
- a real sandbox payout (batch `2XKY33XEAAYVC`, PayPal SUCCESS);
- the limits back at the defaults (version 6);
- the payment log stored in Neon (`leash_render`).

## Left for you

- **Merge into `main`.** This starts the keep-awake ping. It's also the
  branch judges will look at on GitHub.
- **A full run signed in, before judging:** small gift, hamper (then
  approve), try to trick it, hijacked agent. Check that the heading reads
  "gemini · gemini-flash-lite-latest" and not "rule-based agent".
- **Your submission:** add the judge access token (`LEASH_DEMO_TOKEN`) to it,
  and change that token in Render after judging.
- **Optional:** connect Render's GitHub app so a push deploys by itself. I've
  been starting each deploy by hand.
- **Optional:** delete the `leash-tests` Neon branch.
