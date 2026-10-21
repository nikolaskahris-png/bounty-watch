# Bounty Screen

One page. Paste a Superteam Earn listing URL — or any URL, or an on-chain
address — and it screens the things that listing asks you to click or
interact with, using the Webacy / DD.xyz risk APIs, and shows what came back.

## Status — 2026-10-22

**Live: https://bounty-screen.vercel.app** (no login, no signup)

All three endpoints work on the free Demo plan. Verified against live responses:

| Case | Result |
| --- | --- |
| `google.com` | **clean** — `riskLevel: low`, "known as a safe project address" |
| `superteam.fun` | **no data** — `riskLevel: unknown`, "Inconclusive." |
| USDC contract on eth | threat **clean**; contract **flagged** — "State Variable Not Initialized (severity 2)" |
| Tornado Cash wallet | **flagged** — Sanctioned (severity 10), Interacted with Drainer (severity 1) |
| Unused wallet | **no data** — `insufficient_wallet_balance`, `insufficient_wallet_transactions` |
| Wrapped SOL, `chain=sol` | **clean** — threat screening does work on Solana |
| Key kept server-side | verified live: a sentinel key never appeared in any response or the HTML |

### The URL endpoint is mostly "no data", and that is the honest finding

`POST /url` only recognises projects already on its list. `google.com` comes
back `low`; **`superteam.fun` comes back `unknown` / "Inconclusive."**, and so
did an obvious phishing-shaped domain. So for most sponsor links the right
answer is "we have no information about this URL", and the page says exactly
that. It is neither a pass nor a flag.

### Tags carry a numeric severity, and 0 means informational

USDC ships a `Verified Listing` tag at `severity: 0`. An earlier version of
this code flagged any tag at all, which marked USDC as risky and
`superteam.fun` as flagged. Only `severity > 0` counts as a flag now.

### A wallet is not a contract

`GET /contracts` answers `400 "… is not a contract. Use the address risk
endpoint for EOA wallets."` for a plain wallet. The threat call runs first, and
`/contracts` is skipped when it reports `addressType: EOA` — one less quota call
and a clear message instead of an error.

## Read first: what this cannot do

**There is no payout address to screen.** The brief asked for Threat Risks on
the sponsor's payout address. The Superteam listing API exposes no such field:
across 6 live listings the only address-shaped key was `isFndnPaying`, a
boolean, and no listing's description contained an EVM or Solana address. So
Threat Risks is wired up and runs on any address a listing does name, or on an
address you paste directly — and when there is none, the page says so instead
of showing a green tick.

**Contract Risk cannot see Solana.** `GET /contracts/{address}` accepts
`eth, base, bsc, pol, opt, arb, hedera, robinhood` — not `sol` (per Webacy's
own OpenAPI spec). Superteam Earn is Solana-native, so for a Solana address the
page reports that contract screening is unavailable for that chain rather than
pretending it passed. `GET /addresses/{address}` does accept `sol`, so threat
screening works there.

## Endpoints used

| Check | Call |
| --- | --- |
| URL Risk | `POST /url` with `{"url": "..."}` |
| Contract Risk | `GET /contracts/{address}?chain=<evm chain>` |
| Threat Risks | `GET /addresses/{address}?chain=<chain>` |

Base `https://api.webacy.com`, header `x-api-key`. Paths and parameters taken
from <https://docs.webacy.com/openapi.json>, not guessed. DD.xyz is the
front-end for the same risk engine; keys come from developers.webacy.co.

## The key never reaches the browser

The browser only ever calls `/api/screen?q=...` on this site's own origin. The
key is read exclusively inside `api/screen.js` from `process.env.WEBACY_API_KEY`,
which on Vercel is an encrypted environment variable. It is not in the repo,
not in the HTML, and not in any response body — there is a test for that.

This is also why the page is not on GitHub Pages: a static host cannot hold a
secret, so the key would have to go into client-side JavaScript.

## Quota and caching

The free Demo plan is **500 requests/month at 1 request/second**. So:

- **Cache TTL: 24 hours.** Chosen because the inputs are long-lived (a sponsor's
  domain and a contract address do not change hour to hour) while the data is
  not static enough to cache for a week. At 500/month the budget is ~16
  requests/day, so a reviewer re-screening the same listing all afternoon costs
  one request, not fifty.
- Two layers: an in-process `Map` per function instance, plus
  `Cache-Control: public, s-maxage=86400, stale-while-revalidate=604800`, so the
  CDN answers repeat screens of the same input **without invoking the function
  at all**. That is what makes caching survive cold starts without a database.
- **At most 5 API calls per screen** (`MAX_CALLS_PER_SCREEN`). A listing with
  more screenable links says so and screens the first five.
- Calls are spaced 1.1 s apart to respect the 1 req/s limit.
- **Quota exhaustion is stated, never swallowed.** HTTP 402/429 renders as
  "Monthly API quota reached — this check did not run, it is not a pass", and
  screening stops rather than continuing to fail quietly.

## Cost per screen

| Input | Live API calls |
| --- | --- |
| Bare URL | 1 |
| Solana address | 1 (threat only; contract unsupported) |
| EVM address | 2 (threat + contract) |
| Typical listing | 2–5 (sponsor site, contact link, description links) |
| Any repeat within 24 h | 0 |

## The three states

`clean` means the API reported nothing, `flagged` shows the reason the API gave,
and `no data` says plainly that nothing is known. No composite score is invented
on top of their data, and every item can show the raw JSON that produced it.

## Run it

```bash
cp .env.example .env     # add your key; .env is gitignored
node test-local.mjs      # proof harness: real Superteam calls, no key needed
npx vercel dev           # local server
npx vercel --prod        # deploy
```
