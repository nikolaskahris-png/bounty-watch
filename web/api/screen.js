// Server-side screening. The Webacy API key lives in WEBACY_API_KEY and is
// read only here, in the serverless function — it is never sent to the
// browser and never written to the repo.
//
// Endpoints used (paths and params taken from Webacy's published OpenAPI
// spec, https://docs.webacy.com/openapi.json):
//   POST /url                          -> URL Risk       body {url}
//   GET  /contracts/{address}?chain=   -> Contract Risk  EVM chains only
//   GET  /addresses/{address}?chain=   -> Threat Risks   includes sol
//
// Demo plan is 500 requests/month at 1 req/s, so calls are spaced, capped
// per screen, and cached hard.

const WEBACY = "https://api.webacy.com";
const SUPERTEAM = "https://superteam.fun";

const CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24h — see README for why
const MAX_CALLS_PER_SCREEN = 5;           // protects the 500/month budget
const RATE_GAP_MS = 1100;                 // Demo plan is 1 request/second

// Chains the contract endpoint accepts. Solana is NOT among them, which
// matters because Superteam Earn is Solana-native.
const CONTRACT_CHAINS = ["eth", "base", "bsc", "pol", "opt", "arb", "hedera", "robinhood"];

// Per-instance cache. Repeat screens are also absorbed by the CDN via the
// Cache-Control header below, which is what makes this survive cold starts.
const cache = new Map();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function cacheGet(key) {
  const hit = cache.get(key);
  if (!hit) return null;
  if (Date.now() - hit.at > CACHE_TTL_MS) { cache.delete(key); return null; }
  return hit.value;
}
function cacheSet(key, value) {
  cache.set(key, { at: Date.now(), value });
  if (cache.size > 500) cache.delete(cache.keys().next().value);
}

const EVM_RE = /^0x[a-fA-F0-9]{40}$/;
const SOL_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const URL_IN_TEXT = /https?:\/\/[^\s"'<>)]+/g;

function classifyInput(raw) {
  const q = (raw || "").trim();
  if (!q) return { kind: "empty" };
  if (EVM_RE.test(q)) return { kind: "address", address: q, chain: "eth", family: "evm" };
  if (SOL_RE.test(q) && !q.includes(".")) return { kind: "address", address: q, chain: "sol", family: "solana" };

  const withScheme = /^https?:\/\//i.test(q) ? q : `https://${q}`;
  let u;
  try { u = new URL(withScheme); } catch { return { kind: "unknown", value: q }; }

  const m = u.pathname.match(/\/earn\/(?:listing|listings\/[^/]+)\/([^/?#]+)/);
  if (/superteam\.fun$/i.test(u.hostname) && m) return { kind: "listing", slug: m[1], url: u.toString() };
  return { kind: "url", url: u.toString() };
}

async function fetchListing(slug) {
  const r = await fetch(`${SUPERTEAM}/api/listings/details/${encodeURIComponent(slug)}`, {
    headers: { "User-Agent": "bounty-watch-screen/1.0" },
  });
  if (!r.ok) return null;
  return r.json();
}

// Turn a listing into the things a person would actually click or interact
// with. Only what the API really exposes — no invented fields.
function targetsFromListing(listing) {
  const out = [];
  const seen = new Set();
  const add = (t) => {
    const k = `${t.endpoint}:${(t.value || "").toLowerCase()}`;
    if (!t.value || seen.has(k)) return;
    seen.add(k); out.push(t);
  };

  const sponsorUrl = (listing.sponsor || {}).url;
  if (sponsorUrl) {
    add({ endpoint: "url", value: sponsorUrl, label: "Sponsor website",
          why: "The sponsor's own external link, taken from the listing's sponsor.url" });
  }
  if (listing.pocSocials) {
    add({ endpoint: "url", value: listing.pocSocials, label: "Point-of-contact link",
          why: "Where the listing tells you to contact a human (pocSocials)" });
  }
  const desc = listing.description || "";
  for (const link of (desc.match(URL_IN_TEXT) || [])) {
    add({ endpoint: "url", value: link.replace(/[.,;:]+$/, ""), label: "Link in the listing text",
          why: "An external link inside the listing description" });
  }

  // Addresses, if the listing names any. In practice Superteam listings
  // rarely do, and the API exposes no payout address at all.
  const words = desc.replace(/<[^>]+>/g, " ").split(/[\s,"'<>()]+/);
  for (const w of words) {
    if (EVM_RE.test(w)) {
      add({ endpoint: "contract", value: w, chain: "eth", label: "Contract address in the listing",
            why: "An EVM address the listing asks you to interact with" });
      add({ endpoint: "threat", value: w, chain: "eth", label: "Address in the listing",
            why: "Threat screening of the same address" });
    } else if (SOL_RE.test(w) && w.length >= 32) {
      add({ endpoint: "threat", value: w, chain: "sol", label: "Solana address in the listing",
            why: "A Solana address named in the listing" });
    }
  }
  return out;
}

// --- Webacy calls. Each returns a normalised item; none invents a verdict.

async function callWebacy(path, { method = "GET", body } = {}) {
  const key = process.env.WEBACY_API_KEY;
  if (!key) return { transport: "unconfigured" };
  let r;
  try {
    r = await fetch(`${WEBACY}${path}`, {
      method,
      headers: {
        "x-api-key": key,
        ...(body ? { "content-type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    return { transport: "network", message: String(e).slice(0, 200) };
  }
  if (r.status === 402 || r.status === 429) return { transport: "quota", status: r.status };
  if (r.status === 401 || r.status === 403) return { transport: "auth", status: r.status };
  if (r.status === 202) return { transport: "pending", status: 202 };
  let json = null;
  try { json = await r.json(); } catch { /* keep null */ }
  if (!r.ok) return { transport: "error", status: r.status, body: json };
  return { transport: "ok", status: r.status, json };
}

function urlItem(t, res) {
  const base = { ...t, endpoint_called: "POST /url", checked: t.value };
  if (res.transport !== "ok") return { ...base, ...stateFromTransport(res) };
  const d = res.json || {};
  const level = (d.riskLevel ?? "").toString();
  if (!level) {
    return { ...base, state: "nodata", headline: "No information about this URL",
             detail: d.message || d.description || "The API returned no risk level for it.", raw: d };
  }
  const clean = /^(none|low|safe|no risk|clean)$/i.test(level);
  return {
    ...base,
    state: clean ? "clean" : "flagged",
    headline: clean ? `No risk reported (riskLevel: ${level})` : `Flagged — riskLevel: ${level}`,
    detail: d.description || d.message || "",
    raw: d,
  };
}

function contractItem(t, res) {
  const base = { ...t, endpoint_called: `GET /contracts/${t.value}?chain=${t.chain}`, checked: t.value };
  if (res.transport !== "ok") return { ...base, ...stateFromTransport(res) };
  const d = res.json || {};
  const tags = Array.isArray(d.tags) ? d.tags : [];
  const cats = Array.isArray(d.categories) ? d.categories : [];
  if (d.analysis_status && /pending|running|queued/i.test(d.analysis_status)) {
    return { ...base, state: "nodata", headline: "Analysis still running",
             detail: `The API reports analysis_status "${d.analysis_status}" and has nothing final yet.`, raw: d };
  }
  if (!tags.length && !cats.length && d.score === undefined) {
    return { ...base, state: "nodata", headline: "No information about this contract",
             detail: "The API returned no score, tags or categories for it.", raw: d };
  }
  const flagged = tags.length > 0 || cats.length > 0;
  return {
    ...base,
    state: flagged ? "flagged" : "clean",
    headline: flagged
      ? `Flagged — tags: ${[...new Set([...tags, ...cats])].slice(0, 6).join(", ")}`
      : `No tags or categories reported (score: ${d.score})`,
    detail: d.score !== undefined ? `The API's own risk score is ${d.score}.` : "",
    raw: d,
  };
}

function threatItem(t, res) {
  const base = { ...t, endpoint_called: `GET /addresses/${t.value}?chain=${t.chain}`, checked: t.value };
  if (res.transport !== "ok") return { ...base, ...stateFromTransport(res) };
  const d = res.json || {};
  const issues = Array.isArray(d.issues) ? d.issues : [];
  const high = Number(d.high || 0), medium = Number(d.medium || 0);
  const noSignal = !issues.length && !high && !medium &&
                   d.overallRisk === undefined && !d.addressType;
  if (noSignal) {
    return { ...base, state: "nodata",
             headline: "We have no information about this address",
             detail: "The API knows the address but reports no risk data for it.", raw: d };
  }
  const flagged = issues.length > 0 || high > 0 || medium > 0;
  const names = issues.flatMap((i) =>
    (i && i.tags ? i.tags.map((x) => x.name || x.key).filter(Boolean) : [])).slice(0, 6);
  return {
    ...base,
    state: flagged ? "flagged" : "clean",
    headline: flagged
      ? `Flagged — ${high} high / ${medium} medium risk findings` + (names.length ? `: ${names.join(", ")}` : "")
      : "No risk findings reported",
    detail: [
      d.overallRisk !== undefined ? `overallRisk: ${d.overallRisk}` : null,
      d.addressType ? `addressType: ${d.addressType}` : null,
      d.count !== undefined ? `${d.count} transactions seen` : null,
    ].filter(Boolean).join(" · "),
    raw: d,
  };
}

function stateFromTransport(res) {
  switch (res.transport) {
    case "quota":
      return { state: "quota", headline: "Monthly API quota reached",
               detail: `The Webacy Demo plan returned HTTP ${res.status}. This check did not run — it is not a pass.` };
    // These two are different faults and must not read the same, or a
    // rejected key looks like a missing one and gets debugged in the wrong place.
    case "auth":
      return { state: "error", headline: "The screening API rejected the key",
               detail: `HTTP ${res.status} from Webacy — the key is set but invalid, expired, or not entitled to this endpoint.` };
    case "unconfigured":
      return { state: "error", headline: "Screening is not configured on the server",
               detail: "WEBACY_API_KEY is not set in the deployment's environment." };
    case "pending":
      return { state: "nodata", headline: "No result yet",
               detail: "The API accepted the request but has no finished analysis (HTTP 202)." };
    case "network":
      return { state: "error", headline: "Could not reach the screening API", detail: res.message || "" };
    default:
      return { state: "error", headline: `Screening API error (HTTP ${res.status})`,
               detail: typeof res.body === "object" ? JSON.stringify(res.body).slice(0, 300) : "" };
  }
}

export default async function handler(req, res) {
  const q = (req.query.q || "").toString().slice(0, 500);
  const parsed = classifyInput(q);

  // Repeat screens of the same input are served by the CDN without ever
  // invoking this function again, which is the real quota protection.
  res.setHeader("Cache-Control", "public, s-maxage=86400, stale-while-revalidate=604800");

  if (parsed.kind === "empty")
    return res.status(400).json({ error: "Paste a Superteam Earn listing URL, any URL, or an on-chain address." });
  if (parsed.kind === "unknown")
    return res.status(400).json({ error: `Could not read "${q}" as a listing URL, a URL or an address.` });

  const out = { input: q, input_kind: parsed.kind, listing: null, notes: [], items: [], meta: {} };
  let targets = [];

  if (parsed.kind === "listing") {
    const listing = await fetchListing(parsed.slug);
    if (!listing || !listing.slug)
      return res.status(404).json({ error: `No Superteam listing found for "${parsed.slug}".` });
    out.listing = {
      title: listing.title, slug: listing.slug, type: listing.type,
      region: listing.region, sponsor: (listing.sponsor || {}).name,
      sponsor_verified: (listing.sponsor || {}).isVerified,
      sponsor_caution: (listing.sponsor || {}).isCaution,
      reward: listing.rewardAmount, token: listing.token,
      url: `${SUPERTEAM}/earn/listing/${listing.slug}`,
    };
    targets = targetsFromListing(listing);
    // Say plainly what could not be screened, rather than implying it passed.
    if (!targets.some((t) => t.endpoint === "contract"))
      out.notes.push("This listing names no contract address, so there was no contract to screen.");
    if (!targets.some((t) => t.endpoint === "threat"))
      out.notes.push("The Superteam API exposes no payout address for a listing, and this one names no on-chain address, so there was nothing to run threat screening on.");
  } else if (parsed.kind === "url") {
    targets = [{ endpoint: "url", value: parsed.url, label: "The URL you pasted",
                 why: "Checked directly against the URL Risk endpoint" }];
  } else if (parsed.kind === "address") {
    targets = [{ endpoint: "threat", value: parsed.address, chain: parsed.chain,
                 label: "The address you pasted", why: "Threat screening of this address" }];
    if (parsed.family === "evm") {
      targets.push({ endpoint: "contract", value: parsed.address, chain: "eth",
                     label: "The address you pasted, as a contract", why: "Contract risk analysis" });
    } else {
      out.notes.push("Webacy's contract endpoint does not accept Solana (it takes eth, base, bsc, pol, opt, arb, hedera, robinhood), so no contract analysis was run on this address.");
    }
  }

  const budget = targets.slice(0, MAX_CALLS_PER_SCREEN);
  if (targets.length > budget.length)
    out.notes.push(`This listing had ${targets.length} screenable items; only the first ${budget.length} were checked, to stay inside the free Demo quota.`);

  let calls = 0, hits = 0, first = true;
  for (const t of budget) {
    const ck = `${t.endpoint}|${(t.chain || "")}|${t.value.toLowerCase()}`;
    const cached = cacheGet(ck);
    if (cached) { hits++; out.items.push({ ...cached, from_cache: true }); continue; }

    if (!first) await sleep(RATE_GAP_MS); // Demo plan: 1 request per second
    first = false;

    let res2, item;
    if (t.endpoint === "url") {
      res2 = await callWebacy("/url", { method: "POST", body: { url: t.value } });
      item = urlItem(t, res2);
    } else if (t.endpoint === "contract") {
      if (!CONTRACT_CHAINS.includes(t.chain)) {
        item = { ...t, endpoint_called: "GET /contracts (not attempted)", checked: t.value,
                 state: "nodata", headline: "Contract screening is not available for this chain",
                 detail: `The endpoint accepts ${CONTRACT_CHAINS.join(", ")} — not ${t.chain}.` };
        out.items.push(item);
        continue;
      }
      res2 = await callWebacy(`/contracts/${encodeURIComponent(t.value)}?chain=${t.chain}`);
      item = contractItem(t, res2);
    } else {
      res2 = await callWebacy(`/addresses/${encodeURIComponent(t.value)}?chain=${t.chain || "eth"}`);
      item = threatItem(t, res2);
    }
    calls++;
    if (item.state !== "error" && item.state !== "quota") cacheSet(ck, item);
    out.items.push(item);
    if (item.state === "quota") {
      out.notes.push("Screening stopped early: the monthly API quota is exhausted.");
      break;
    }
  }

  out.meta = {
    api_calls_made: calls,
    served_from_cache: hits,
    cache_ttl_hours: CACHE_TTL_MS / 3600000,
    max_calls_per_screen: MAX_CALLS_PER_SCREEN,
  };
  return res.status(200).json(out);
}
