// Local proof harness. Runs the real serverless handler against the real
// Superteam API, with no Webacy key, so everything except the Webacy calls
// is exercised for real. Run: node test-local.mjs
import handler from "./api/screen.js";

const CASES = [
  ["listing URL", "https://superteam.fun/earn/listing/vaultline-security-audit-grants-for-colosseum-projects-up-to-4000-usdc-each"],
  ["listing URL με trailing slash", "https://superteam.fun/earn/listing/bring-your-friends-to-segmento/"],
  ["γυμνό URL", "https://superteam.fun"],
  ["URL χωρίς scheme", "daejin.xyz"],
  ["EVM address", "0x0000000000000000000000000000000000000000"],
  ["Solana address", "So11111111111111111111111111111111111111112"],
  ["σκουπίδια", "αυτο δεν ειναι τιποτα"],
  ["κενό", ""],
  ["ανύπαρκτο listing", "https://superteam.fun/earn/listing/den-yparxei-auto-to-slug-12345"],
];

function fakeRes() {
  const r = { headers: {}, code: null, body: null };
  r.setHeader = (k, v) => { r.headers[k] = v; };
  r.status = (c) => { r.code = c; return r; };
  r.json = (b) => { r.body = b; return r; };
  return r;
}

const rows = [];
for (const [name, q] of CASES) {
  const res = fakeRes();
  let err = null;
  try { await handler({ query: { q } }, res); } catch (e) { err = e; }
  const b = res.body || {};
  const states = (b.items || []).map((i) => i.state);
  const counts = states.reduce((a, s) => ((a[s] = (a[s] || 0) + 1), a), {});
  rows.push({
    name, http: res.code, crashed: err ? String(err).slice(0, 60) : "",
    kind: b.input_kind || "-",
    listing: b.listing ? b.listing.title.slice(0, 28) : "-",
    items: (b.items || []).length,
    states: JSON.stringify(counts),
    notes: (b.notes || []).length,
    error: b.error ? b.error.slice(0, 48) : "",
    cacheHdr: (res.headers["Cache-Control"] || "").includes("s-maxage=86400") ? "yes" : "NO",
  });
}

const w = (s, n) => String(s).padEnd(n).slice(0, n);
console.log("\n" + w("case", 30) + w("http", 6) + w("kind", 9) + w("listing", 30) +
            w("items", 6) + w("states", 22) + w("notes", 6) + w("cacheHdr", 9));
console.log("-".repeat(118));
for (const r of rows) {
  console.log(w(r.name, 30) + w(r.http, 6) + w(r.kind, 9) + w(r.listing, 30) +
              w(r.items, 6) + w(r.states, 22) + w(r.notes, 6) + w(r.cacheHdr, 9) +
              (r.crashed ? "  CRASH:" + r.crashed : "") + (r.error ? "  " + r.error : ""));
}

// Prove the key is never echoed back to the browser, even when it is set.
process.env.WEBACY_API_KEY = "SECRET-SENTINEL-abc123";
const res2 = fakeRes();
await handler({ query: { q: "https://superteam.fun/earn/listing/bring-your-friends-to-segmento" } }, res2);
const blob = JSON.stringify(res2.body);
console.log("\n=== διαρροή κλειδιού στην απάντηση; ===");
console.log(blob.includes("SECRET-SENTINEL") ? "  ΑΠΟΤΥΧΙΑ: το κλειδί εμφανίζεται!" :
            "  PASS: το sentinel κλειδί δεν υπάρχει πουθενά στο JSON της απάντησης");
console.log("  states με άκυρο κλειδί:",
  JSON.stringify((res2.body.items || []).map((i) => [i.state, i.headline])));

// Extraction detail for one real listing.
const res3 = fakeRes();
delete process.env.WEBACY_API_KEY;
await handler({ query: { q: "https://superteam.fun/earn/listing/bring-your-friends-to-segmento" } }, res3);
console.log("\n=== τι εντόπισε σε πραγματικό listing ===");
for (const i of res3.body.items || []) console.log(`  [${i.endpoint}] ${i.label}: ${i.checked}`);
for (const n of res3.body.notes || []) console.log("  σημείωση:", n);
