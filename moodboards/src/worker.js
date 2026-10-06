// NicVisions client moodboards.
//
// Every request runs through this Worker (assets.run_worker_first), so nothing
// under public/ is ever served without a check. Cloudflare Access sits in
// front of the whole site and makes each visitor prove their email with a
// one-time code; this Worker then verifies Access's signed token and only
// shows a board to the client whose email is on it (or to an admin).
//
// Routes (token = the board's private link id):
//   GET  /m/<token>/            the moodboard page
//   GET  /m/<token>/board.json  the board's details
//   GET  /m/<token>/p/<file>    one of the board's photos
//   GET  /m/<token>/status      { confirmedAt } for this board
//   GET  /m/<token>/event/<hmua|shoot>.ics   calendar file (Apple Calendar)
//   POST /m/<token>/confirm     the client confirms they're happy
//
// Confirmations are written to the private GitHub repo
// (moodboards/confirmations/<token>.json) so the studio can pick them up.

const TOKEN_RE = /^[A-Za-z0-9_-]{16,64}$/;
const FILE_RE = /^[A-Za-z0-9_-]{1,80}\.(jpg|png)$/;
let certCache = { at: 0, keys: null };
const devConfirmations = {}; // local testing only

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);

    if (parts.length === 0) return page(200, "NicVisions moodboards", "Open the link from your email to see your moodboard.");
    if (parts[0] !== "m" || !TOKEN_RE.test(parts[1] || "")) return page(404, "Page not found", "Check the link in your email and try again.");

    const token = parts[1];
    const email = await accessEmail(request, env);
    if (!email) return page(401, "Please sign in", "Open the link from your email again and enter the code we send you.");

    const board = await loadBoard(env, url, token);
    if (!board) return page(404, "Moodboard not found", "This moodboard may have been replaced. Check the latest email from Nicole.");
    if (!allowed(email, board, env)) {
      return page(403, "This moodboard is for someone else",
        "You’re signed in as " + escapeHtml(email) + ". Sign in with the email address Nicole sent the link to.");
    }

    const rest = parts.slice(2);
    const noStore = { "Cache-Control": "private, no-store" };

    if (rest.length === 0 && request.method === "GET") {
      if (!url.pathname.endsWith("/")) return Response.redirect(url.origin + url.pathname + "/", 301);
      const res = await env.ASSETS.fetch(new URL("/viewer.html", url));
      return new Response(res.body, { headers: { "Content-Type": "text/html; charset=utf-8", ...noStore } });
    }
    if (rest[0] === "board.json" && request.method === "GET") {
      return json(board, 200, noStore);
    }
    if (rest[0] === "p" && FILE_RE.test(rest[1] || "") && request.method === "GET") {
      const res = await env.ASSETS.fetch(new URL("/data/" + token + "/" + rest[1], url));
      if (!res.ok) return new Response("Not found", { status: 404 });
      return new Response(res.body, { headers: { "Content-Type": rest[1].endsWith(".png") ? "image/png" : "image/jpeg", ...noStore } });
    }
    if (rest[0] === "event" && /^(hmua|shoot)\.ics$/.test(rest[1] || "") && request.method === "GET") {
      const kind = rest[1].slice(0, -4), ics = calendarFile(board, kind, token);
      if (!ics) return page(404, "No date yet", "This appointment doesn’t have a date and time yet.");
      return new Response(ics, { headers: { "Content-Type": "text/calendar; charset=utf-8",
        "Content-Disposition": 'attachment; filename="nicvisions-' + (kind === "hmua" ? "hair-and-makeup" : "photoshoot") + '.ics"', ...noStore } });
    }
    if (rest[0] === "status" && request.method === "GET") {
      const c = await readConfirmation(env, token);
      return json({ confirmedAt: c ? c.confirmedAt : null }, 200, noStore);
    }
    if (rest[0] === "confirm" && request.method === "POST") {
      const existing = await readConfirmation(env, token);
      if (existing) return json({ confirmedAt: existing.confirmedAt }, 200, noStore);
      const record = { token, boardId: board.boardId || "", confirmedAt: new Date().toISOString(), confirmedBy: email };
      const ok = await writeConfirmation(env, token, record);
      return ok ? json({ confirmedAt: record.confirmedAt }, 200, noStore) : json({ error: "not_saved" }, 502, noStore);
    }
    return page(404, "Page not found", "Check the link in your email and try again.");
  },
};

function allowed(email, board, env) {
  const e = email.trim().toLowerCase();
  if ((board.clientEmail || "").trim().toLowerCase() === e) return true;
  return String(env.ADMIN_EMAILS || "").split(",").map((x) => x.trim().toLowerCase()).filter(Boolean).includes(e);
}

async function loadBoard(env, url, token) {
  const res = await env.ASSETS.fetch(new URL("/data/" + token + "/board.json", url));
  if (!res.ok) return null;
  try { return await res.json(); } catch { return null; }
}

// ---------- Cloudflare Access ----------
// Fails closed: without TEAM_DOMAIN and ACCESS_AUD configured, nobody gets in.
async function accessEmail(request, env) {
  if (env.DEV_ACCESS_EMAIL) return env.DEV_ACCESS_EMAIL; // local testing only, never set in production
  if (!env.TEAM_DOMAIN || !env.ACCESS_AUD) return null;
  const jwt = request.headers.get("Cf-Access-Jwt-Assertion") || cookie(request, "CF_Authorization");
  if (!jwt) return null;
  const segs = jwt.split(".");
  if (segs.length !== 3) return null;
  let header, payload;
  try { header = JSON.parse(b64urlText(segs[0])); payload = JSON.parse(b64urlText(segs[1])); } catch { return null; }
  if (header.alg !== "RS256") return null;
  const now = Math.floor(Date.now() / 1000);
  if (!payload.exp || payload.exp < now) return null;
  const aud = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (!aud.includes(env.ACCESS_AUD)) return null;
  if (payload.iss && payload.iss !== "https://" + env.TEAM_DOMAIN) return null;
  const key = await accessKey(env, header.kid);
  if (!key) return null;
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64urlBytes(segs[2]), new TextEncoder().encode(segs[0] + "." + segs[1]));
  return ok && payload.email ? String(payload.email) : null;
}

async function accessKey(env, kid) {
  if (!certCache.keys || Date.now() - certCache.at > 3600000) {
    const res = await fetch("https://" + env.TEAM_DOMAIN + "/cdn-cgi/access/certs");
    if (!res.ok) return null;
    const { keys } = await res.json();
    certCache = { at: Date.now(), keys: keys || [] };
  }
  const jwk = certCache.keys.find((k) => k.kid === kid);
  if (!jwk) return null;
  return crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
}

// ---------- confirmations in the private repo ----------
function ghHeaders(env) {
  return { Authorization: "Bearer " + env.GITHUB_TOKEN, Accept: "application/vnd.github+json", "User-Agent": "nicvisions-moodboards" };
}
function confirmationUrl(env, token) {
  return "https://api.github.com/repos/" + env.GITHUB_REPO + "/contents/moodboards/confirmations/" + token + ".json";
}
async function readConfirmation(env, token) {
  if (env.DEV_ACCESS_EMAIL) return devConfirmations[token] || null; // local testing only
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) return null;
  const res = await fetch(confirmationUrl(env, token) + "?ref=main", { headers: ghHeaders(env) });
  if (!res.ok) return null;
  const file = await res.json();
  try { return JSON.parse(atob(String(file.content || "").replace(/\s/g, ""))); } catch { return null; }
}
async function writeConfirmation(env, token, record) {
  if (env.DEV_ACCESS_EMAIL) { devConfirmations[token] = record; return true; }
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) return false;
  const body = {
    message: "Moodboard confirmed",
    content: btoa(JSON.stringify(record, null, 2) + "\n"),
    branch: "main",
  };
  const res = await fetch(confirmationUrl(env, token), { method: "PUT", headers: { ...ghHeaders(env), "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return res.ok || res.status === 422; // 422: already exists, so already confirmed
}

// ---------- calendar files ----------
// Board times are Adelaide local times. Keep the wording in step with
// calEvent() in the client page.
const EVENT_TZ = "Australia/Adelaide";
const EVENT_MINUTES = { hmua: 90, shoot: 120 };
function tzOffset(ms) {
  const p = {};
  new Intl.DateTimeFormat("en-US", { timeZone: EVENT_TZ, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })
    .formatToParts(new Date(ms)).forEach((x) => { p[x.type] = x.value; });
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) - ms;
}
function localToUtc(v) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(v || "");
  if (!m) return null;
  const wall = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  let t = wall - tzOffset(wall);
  t = wall - tzOffset(t);
  return new Date(t);
}
function icsText(s) { return String(s || "").replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/\r?\n/g, "\\n"); }
function fold(line) {
  const out = []; let cur = "";
  for (const ch of line) { if (new TextEncoder().encode(cur + ch).length > 74) { out.push(cur); cur = " " + ch; } else cur += ch; }
  out.push(cur); return out.join("\r\n");
}
function calendarFile(b, kind, token) {
  const start = localToUtc(kind === "hmua" ? b.hmuaAt : b.shootAt);
  if (!start) return null;
  const end = new Date(start.getTime() + EVENT_MINUTES[kind] * 60000);
  const stamp = (d) => d.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  const title = kind === "hmua" ? "Hair & makeup" + (b.hmuaArtist ? " with " + b.hmuaArtist : "") + " · NicVisions shoot" : "Your Show Up shoot with NicVisions";
  const details = kind === "hmua"
    ? "Hair and makeup before your Show Up shoot with NicVisions." + (b.hmuaArtist ? "\nArtist: " + b.hmuaArtist : "") + (b.hmuaContact ? "\nContact: " + b.hmuaContact : "")
    : "Your post-show photoshoot with Nicole from NicVisions Photography. You just show up.";
  const location = kind === "hmua" ? b.hmuaAddress : b.shootAddress;
  return ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//NicVisions Photography//Moodboard//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
    "BEGIN:VEVENT", "UID:" + token + "-" + kind + "@nicvisions", "DTSTAMP:" + stamp(new Date()),
    "DTSTART:" + stamp(start), "DTEND:" + stamp(end), "SUMMARY:" + icsText(title),
    location ? "LOCATION:" + icsText(location) : "", "DESCRIPTION:" + icsText(details),
    "END:VEVENT", "END:VCALENDAR"].filter(Boolean).map(fold).join("\r\n") + "\r\n";
}

// ---------- helpers ----------
function cookie(request, name) {
  const m = (request.headers.get("Cookie") || "").match(new RegExp("(?:^|;\\s*)" + name + "=([^;]+)"));
  return m ? m[1] : null;
}
function b64urlBytes(s) {
  const b = atob(s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "="));
  return Uint8Array.from(b, (c) => c.charCodeAt(0));
}
function b64urlText(s) { return new TextDecoder().decode(b64urlBytes(s)); }
function json(obj, status, headers) {
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...headers } });
}
function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function page(status, title, text) {
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(title)} · NicVisions</title><meta name="robots" content="noindex">
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f2ee;color:#1f1412;font:16px/1.6 "Manrope",system-ui,sans-serif;padding:24px;box-sizing:border-box}
main{max-width:30rem}h1{font:500 32px/1.15 "Bodoni Moda",Didot,Georgia,serif;margin:0 0 10px}p{margin:0;color:#5d4c46}.w{font:20px "Bodoni Moda",Georgia,serif;margin-bottom:28px}
@media (prefers-color-scheme:dark){body{background:#1a1312;color:#fbf7f3}p{color:#d4c4bb}}</style></head>
<body><main><div class="w">NicVisions</div><h1>${escapeHtml(title)}</h1><p>${text}</p></main></body></html>`;
  return new Response(html, { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex" } });
}
