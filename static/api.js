/* ==========================================================================
   GearHeadSpecs — shared client helpers.

   esc() matters more here than it did in the mockups. There, every string
   rendered through innerHTML was a hardcoded literal the author wrote. Now
   spec values, alternates, link titles, flag details and usernames all come
   from other people, and any of them reaching innerHTML unescaped is stored
   XSS. Every interpolation of server data goes through esc().
   ========================================================================== */

function esc(v){
  if(v === null || v === undefined) return "";
  return String(v)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* A URL is still attacker-supplied even after escaping — esc() stops it
   breaking out of the attribute, not `javascript:` from running on click. */
function safeUrl(v){
  const s = String(v || "").trim();
  return /^https?:\/\//i.test(s) ? s : "#";
}

/* A spec mirrored under a second heading is the same row printed twice, so
   every id inside it exists twice on the page. Lookups go through byId(),
   which prefers the copy the click happened in: SCOPE is the mirror the
   event started inside, or null for the original. Set in the capture phase
   so every handler, in every script, sees it. */
let SCOPE = null;
["click", "input", "change"].forEach(t =>
  document.addEventListener(t, (e) => {
    SCOPE = e.target && e.target.closest ? e.target.closest(".mirror") : null;
  }, true));
function byId(id){
  if(SCOPE){
    const hit = SCOPE.querySelector("#" + CSS.escape(id));
    if(hit) return hit;
  }
  return document.getElementById(id);
}
function scopeAll(sel){ return (SCOPE || document).querySelectorAll(sel); }
function scopeOne(sel){ return (SCOPE || document).querySelector(sel); }

async function api(method, path, body){
  const opts = { method, headers: {}, credentials: "same-origin" };
  if(body !== undefined){
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch(e){ /* empty body */ }
  if(!res.ok){
    const err = new Error(data.error || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

const API = {
  get:    (p)    => api("GET", p),
  post:   (p, b) => api("POST", p, b || {}),
  patch:  (p, b) => api("PATCH", p, b || {}),
  del:    (p, b) => api("DELETE", p, b),
};

let toastTimer = null;

/* Notes stay for 30 seconds, or until dismissed.
   Several of these report something worth reading — how many bikes a field
   landed on, why a value was refused — and at four seconds they were gone
   before you reached the end of the sentence. */
const TOAST_MS = 30000;

function toast(message, kind){
  document.querySelectorAll(".toast").forEach(t => t.remove());
  clearTimeout(toastTimer);

  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");

  const text = document.createElement("span");
  text.className = "toast-text";
  // textContent, not innerHTML — these messages quote values people typed.
  text.textContent = message;
  el.appendChild(text);

  const close = document.createElement("button");
  close.className = "toast-close";
  close.type = "button";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "×";
  el.appendChild(close);

  // Clicking anywhere on it dismisses, so the × is a signpost rather than a
  // small target you have to hit.
  el.addEventListener("click", () => {
    clearTimeout(toastTimer);
    el.remove();
  });

  document.body.appendChild(el);
  toastTimer = setTimeout(() => el.remove(), TOAST_MS);
}

/* Any unhandled API rejection should say something, not fail silently. */
function guard(fn){
  return async (...args) => {
    try { return await fn(...args); }
    catch(e){
      if(e.status === 401){
        // Long enough to read before the page changes under them. The
        // redirect is the resolution, so this does not need the full 30s.
        toast("Session expired — taking you to the sign-in page.", "error");
        setTimeout(() => location.href = "/login.html?next=" +
          encodeURIComponent(location.pathname), 3000);
        return;
      }
      toast(e.message || "Something went wrong.", "error");
    }
  };
}

/* ---------------------------------------------------------------------------
   Session + nav. Every page calls initChrome() and gets the same header,
   the same nav, and the same answer to "who am I".
   --------------------------------------------------------------------------- */
let ME = { user: null, manages: [] };

const NAV = [
  { href: "/index.html",         label: "Browse",         role: "anon" },
  { href: "/manager.html",       label: "My Bikes",       role: "manager" },
  { href: "/spec-tree.html",     label: "Spec Tree",      role: "manager" },
  { href: "/board.html",         label: "Board",          role: "manager", count: "board" },
  { href: "/messages.html",      label: "Messages",       role: "manager", count: "messages" },
  { href: "/questionnaire.html", label: "Add a Bike",     role: "admin" },
  { href: "/add-spec.html",      label: "Add a Spec",     role: "admin" },
  { href: "/admin.html",         label: "Admin",          role: "admin" },
];

const RANK = { user: 0, manager: 1, admin: 2 };

function canSee(role){
  if(role === "anon") return true;
  if(!ME.user) return false;
  return RANK[ME.user.role] >= RANK[role];
}

function signedIn(){ return !!ME.user; }

/* ---------------------------------------------------------------------------
   Anonymous visitors.

   A visitor who is not signed in can read the whole database but change none
   of it. The controls stay VISIBLE and clickable rather than being hidden —
   hiding them means a first-time reader never learns the site is editable at
   all. Clicking one explains what it would do and offers a way in.

   The interception runs in the CAPTURE phase so it fires before the per-page
   click handlers further down the tree, and stops the event there. Without
   that, each page would have to remember to re-check on every single action,
   and the one that forgot would be the security hole.
   --------------------------------------------------------------------------- */
function authAttr(){
  // Stamped on any control that writes. Harmless when signed in.
  return signedIn() ? "" : ' data-needs-auth';
}

const AUTH_MESSAGES = {
  vote: "Voting tells everyone which answer actually worked.",
  flag: "Flagging sends this to the rider who maintains this bike.",
  value: "Adding a value puts it on the page for every rider who looks this bike up.",
  garage: "A garage keeps your bikes and service history on your account.",
  default: "Contributing to the spec sheets needs an account.",
};

function showAuthPrompt(el){
  document.querySelectorAll(".auth-prompt").forEach(p => p.remove());
  const kind = el.dataset.authKind || "default";
  const host = el.closest(".spec-row, .link-row, .garage-bike, .panel-body, .panel")
            || el.parentElement;
  const box = document.createElement("div");
  box.className = "auth-prompt";
  box.innerHTML = `
    <div class="auth-prompt-title">Sign in to do that</div>
    <div class="auth-prompt-body">${esc(AUTH_MESSAGES[kind] || AUTH_MESSAGES.default)}
      You can keep reading every spec on the site without an account.</div>
    <div class="auth-prompt-actions">
      <a class="primary-btn" href="/login.html?next=${
        encodeURIComponent(location.pathname + location.search)}">Sign in</a>
      <a class="action-btn" href="/register.html?next=${
        encodeURIComponent(location.pathname + location.search)}">Create account</a>
      <button class="ghost-btn" data-auth-dismiss>Not now</button>
    </div>`;
  host.insertAdjacentElement("afterend", box);
  box.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

document.addEventListener("click", (e) => {
  if(e.target.closest("[data-auth-dismiss]")){
    document.querySelectorAll(".auth-prompt").forEach(p => p.remove());
    e.preventDefault();
    e.stopPropagation();
    return;
  }
  if(signedIn()) return;
  const el = e.target.closest("[data-needs-auth]");
  if(!el) return;
  e.preventDefault();
  e.stopPropagation();
  showAuthPrompt(el);
}, true);

async function initChrome(activeHref){
  try { ME = await API.get("/api/auth/me"); }
  catch(e){ ME = { user: null, manages: [] }; }

  const nav = document.createElement("nav");
  nav.className = "mainnav";
  // A count beside Board and Messages: threads with posts you have not
  // seen, messages you have not opened. Zero shows nothing.
  const links = NAV.filter(n => canSee(n.role))
    .map(n => {
      const c = n.count && ME.unread ? ME.unread[n.count] : 0;
      return `<a href="${n.href}"${n.href === activeHref ? ' class="active"' : ""}>${esc(n.label)}${
        c ? `<span class="nav-count">${c}</span>` : ""}</a>`;
    })
    .join("");

  const right = ME.user
    ? `<span>${esc(ME.user.display_name || ME.user.username)} · ${esc(ME.user.role)}</span>
       <a href="/password.html" class="action-btn" style="text-decoration:none" title="Change your password">Password</a>
       <button class="action-btn" id="logout-btn">Sign out</button>`
    : `<a href="/login.html?next=${encodeURIComponent(location.pathname)}">Sign in</a>`;

  nav.innerHTML = links + `<div class="nav-right">${right}</div>`;
  const header = document.querySelector("header");
  header.parentNode.insertBefore(nav, header.nextSibling);

  // Admin signed in as a member for testing: say so on every page, with
  // the way back. The server only reports this while the admin session
  // behind it is still live.
  if(ME.testing_as){
    const bar = document.createElement("div");
    bar.className = "testing-bar";
    bar.innerHTML = `<span>Testing as <strong>${esc(ME.user.username)}</strong> (${esc(ME.user.role)}) —
      you are seeing the site as they do. Signed in as admin <strong>${esc(ME.testing_as.admin)}</strong> underneath.</span>
      <button class="action-btn" id="back-to-admin">Back to admin</button>`;
    nav.parentNode.insertBefore(bar, nav.nextSibling);
    document.getElementById("back-to-admin").addEventListener("click", guard(async () => {
      await API.post("/api/auth/return");
      location.href = "/admin.html#p-members";
    }));
  }

  const logout = document.getElementById("logout-btn");
  if(logout){
    logout.addEventListener("click", guard(async () => {
      await API.post("/api/auth/logout");
      location.href = "/index.html";
    }));
  }
  return ME;
}

/* Keep the count beside a nav link honest as the page reads things: the
   nav was drawn on load, before the thread was opened. */
function setNavCount(href, n){
  const a = [...document.querySelectorAll("nav.mainnav a")].find(x => x.getAttribute("href") === href);
  if(!a) return;
  let badge = a.querySelector(".nav-count");
  if(!n){ if(badge) badge.remove(); return; }
  if(!badge){ badge = document.createElement("span"); badge.className = "nav-count"; a.appendChild(badge); }
  badge.textContent = n;
}

/* Pages that only make sense signed in as a particular role. The server
   enforces this too — this is just so the user gets a sentence instead of a
   page full of 403s. */
function requireRole(role){
  if(canSee(role)) return true;
  const main = document.querySelector("main");
  main.innerHTML = ME.user
    ? `<div class="panel"><div class="panel-body empty">
         This page needs <strong>${esc(role)}</strong> access.
         You are signed in as ${esc(ME.user.username)} (${esc(ME.user.role)}).
       </div></div>`
    : `<div class="panel"><div class="panel-body empty">
         <p>Sign in to use this page.</p>
         <a class="primary-btn" style="text-decoration:none;padding:9px 18px"
            href="/login.html?next=${encodeURIComponent(location.pathname)}">Sign in</a>
       </div></div>`;
  return false;
}

function confidenceBadge(conf){
  if(!conf) return "";
  const label = { confirmed: "Manual-confirmed", mfr: "Mfr-sourced",
                  pending: "Pending source" }[conf] || conf;
  return `<span class="badge conf-${esc(conf)}">${esc(label)}</span>`;
}

function typeBadge(type){
  // 'pref' is the default now, so badging it would put a chip on almost every
  // row and say nothing. A badge should mark the exception.
  if(!type || type === "pref") return "";
  const label = { fixed: "Fixed spec", community: "Community-driven" }[type] || type;
  return `<span class="badge type-${esc(type)}">${esc(label)}</span>`;
}

function fmtDate(s){
  if(!s) return "—";
  const d = new Date(s.replace(" ", "T") + (s.includes("T") ? "" : "Z"));
  if(isNaN(d)) return s;
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if(days <= 0) return "today";
  if(days === 1) return "yesterday";
  if(days < 30) return `${days} days ago`;
  return d.toISOString().slice(0, 10);
}

const THUMB_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2h.5a2.5 2.5 0 0 1 2.5 2.5v1.38Z"/></svg>`;
const FLAG_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 22V4a1 1 0 0 1 .4-.8A6 6 0 0 1 8 2c2.5 0 4 1.5 6 1.5s3-.5 4.5-1.5a.5.5 0 0 1 .8.4v10.6a.5.5 0 0 1-.8.4c-1.5 1-3 1.5-4.5 1.5-2 0-3.5-1.5-6-1.5a6 6 0 0 0-3.6.7"/></svg>`;
