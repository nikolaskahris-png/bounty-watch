# Bounty Screen

One page. Paste a Superteam Earn listing URL — or any URL, or an on-chain
address — and it screens the things that listing asks you to click or
interact with, using the Webacy / DD.xyz risk APIs, and shows what came back.

## Status — 2026-10-21

| Part | State |
| --- | --- |
| Input parsing (listing URL, bare URL, schemeless domain, EVM, Solana, junk) | verified locally against the live Superteam API |
| Listing fetch + target extraction | verified: 5 screenable links found on a real listing |
| API key kept server-side | verified: a sentinel key never appears in the response |
| Endpoint paths | verified to exist (401 with an invalid key; a nonexistent path gives 403) |
| Webacy response handling | **unverified — needs a real Demo key** |
| Deployment | **not deployed yet — needs hosting auth** |

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
