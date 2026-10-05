// Pieces every view of the Ixel window shares: building DOM, icons, the session key and the
// API. Everything a model or the server sends is untrusted: it only ever becomes text nodes,
// never HTML.

// ── DOM helpers ───────────────────────────────────────────────────────────

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

// Builds an element. Children that are strings become text nodes. Attribute
// names are always literals in this file; values may be data (text only).
export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// Line icons on a 24×24 grid, built as SVG nodes from the fixed paths below
// (never parsed from markup).
const SVG_NS = "http://www.w3.org/2000/svg";
export const ICONS = {
  plus: "M12 5v14M5 12h14",
  arrowUp: "M12 19V5M5.5 11.5 12 5l6.5 6.5",
  stop: "M8 7h8a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z",
  copy: "M9 9h10a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V10a1 1 0 0 1 1-1zM5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1",
  check: "M20 6 9 17l-5-5",
  x: "M18 6 6 18M6 6l12 12",
  chevron: "m6 9 6 6 6-6",
  menu: "M4 7h16M4 12h16M4 17h16",
  lock: "M6.5 11h11a1.5 1.5 0 0 1 1.5 1.5v7a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 19.5v-7A1.5 1.5 0 0 1 6.5 11zM8 11V7.5a4 4 0 0 1 8 0V11",
  alert: "M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01",
  info: "M12 3a9 9 0 1 0 0 18 9 9 0 1 0 0-18zM12 16v-4M12 8h.01",
  trophy: "M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0V4zM7 6H4.5v1A3.5 3.5 0 0 0 8 10.5M17 6h2.5v1a3.5 3.5 0 0 1-3.5 3.5",
  reply: "M4 5v6a4 4 0 0 0 4 4h12M15 10l5 5-5 5",
  scale: "M12 3v18M8 21h8M5 7h14M12 3l-1 4h2zM5 7l-3 7a3 3 0 0 0 6 0L5 7zM19 7l-3 7a3 3 0 0 0 6 0l-3-7z",
  message: "M5 5h14a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H10l-5 4v-4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z",
  zap: "M13 3 5 13.5h6L10 21l8-10.5h-6L13 3z",
  skip: "M5 12h14",
  code: "m8 8-5 4 5 4M16 8l5 4-5 4M14 5l-4 14",
  pulse: "M3 12h4l2.5-6 5 12 2.5-6H21",
  refresh: "M20 11a8 8 0 1 0-2.34 5.66M20 4v7h-7",
  circle: "M12 4.5a7.5 7.5 0 1 0 0 15 7.5 7.5 0 1 0 0-15z",
  board: "M4 4h4.5v16H4zM9.75 4h4.5v10h-4.5zM15.5 4H20v7h-4.5z",
  play: "M8 5.5v13l10.5-6.5z",
  folder: "M3.5 7V5.5A1.5 1.5 0 0 1 5 4h4l2 2.5h8A1.5 1.5 0 0 1 20.5 8v10a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18z",
  file: "M14 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7.5L14 3zM14 3v4.5h4.5",
  sliders: "M20 5h-7M9 5H4M20 12h-9M7 12H4M20 19h-5M11 19H4M13 3v4M7 10v4M15 17v4",
  image: "M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zM8.5 7.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 1 0 0-3zM20 15l-5-5L5 20",
  pull: "M6 3.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 1 0 0-5zM6 15.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 1 0 0-5zM18 15.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 1 0 0-5zM6 8.5v7M18 15.5V9a2 2 0 0 0-2-2h-5M13.5 4.5 11 7l2.5 2.5",
  server: "M5 4h14a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zM5 14h14a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-4a1 1 0 0 1 1-1zM8 7h.01M8 17h.01",
  terminal: "M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zM7 10l3 2.5L7 15M12.5 15H17",
  mic: "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7",
  book: "M12 6.5C10.5 5 8 4.5 4 4.5v14c4 0 6.5.5 8 2 1.5-1.5 4-2 8-2v-14c-4 0-6.5.5-8 2zM12 6.5v14",
};

export function icon(name, cls) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", `icon icon-${name}${cls ? ` ${cls}` : ""}`);
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

// ── Small shared pieces ───────────────────────────────────────────────────

export const secs = (ms) => (ms < 10_000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms / 1000)}s`);
export const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    // Older browsers, or the clipboard permission is off: copy from a hidden box
    const box = el("textarea", { class: "sr-only", "aria-hidden": "true" });
    box.value = text;
    document.body.append(box);
    box.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
    box.remove();
    return ok;
  }
}

export function copyButton(getText, label) {
  const button = el("button", { type: "button", class: "icon-btn copy", "aria-label": label, title: label }, icon("copy"));
  button.addEventListener("click", async () => {
    const ok = await copyText(getText());
    button.replaceChildren(icon(ok ? "check" : "x"));
    button.classList.toggle("copied", ok);
    setTimeout(() => { button.replaceChildren(icon("copy")); button.classList.remove("copied"); }, 1400);
  });
  return button;
}

export function fillIcons(root = document) {
  for (const slot of $$("i[data-icon]", root)) slot.replaceWith(icon(slot.dataset.icon));
}

// ── Session token ─────────────────────────────────────────────────────────

const TOKEN_KEY = "ixel-session";

function readToken() {
  const match = location.hash.match(/token=([A-Za-z0-9_-]+)/);
  if (match) {
    try { sessionStorage.setItem(TOKEN_KEY, match[1]); } catch (e) { /* private mode */ }
    history.replaceState(null, "", location.pathname);  // keep the key out of the address bar
    return match[1];
  }
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}

export const token = readToken();

// A request to Ixel with the session key. A text body is JSON; anything else (a picture) says its own type.
export function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: `Bearer ${token}` };
  if (typeof options.body === "string" && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  return fetch(path, { ...options, headers, cache: "no-store" });
}

// Held open for as long as this page is: `ixel app` stops its server once its window is closed
export async function stayPresent(retry = 1000) {
  try {
    const res = await api("/api/presence");
    if (res.ok && res.body) {
      const reader = res.body.getReader();
      while (!(await reader.read()).done) { /* a keep-alive line every 15 s */ }
      retry = 1000;
    }
  } catch (e) { /* the server stopped, or this computer slept */ }
  setTimeout(() => stayPresent(Math.min(retry * 2, 30000)), retry);
}

// The project folder last used with Handoff (Ask's /handoff and the Board share it)
const HANDOFF_PROJECT_KEY = "ixel.handoff.project";

export function rememberedProject() {
  try { return localStorage.getItem(HANDOFF_PROJECT_KEY) || ""; } catch (e) { return ""; }
}

export function rememberProject(path) {
  try { localStorage.setItem(HANDOFF_PROJECT_KEY, path); } catch (e) { /* storage is off */ }
}

// A failed request's error carries the server's words, and its code when it gave one
function failed(res, data) {
  const error = new Error(data.error || `Request failed (${res.status})`);
  error.status = res.status;
  error.code = typeof data.code === "string" ? data.code : "";
  error.data = data;
  return error;
}

export async function getJSON(path) {
  const res = await api(path);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw failed(res, data);
  return data;
}

export async function postJSON(path, body) {
  const res = await api(path, { method: "POST", body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw failed(res, data);
  return data;
}

// "3 min ago" for one of Handoff's times (2026-10-03T05:00:00Z)
export function ago(when, now = Date.now()) {
  const at = Date.parse(when);
  if (Number.isNaN(at)) return "";
  const s = Math.max(0, Math.round((now - at) / 1000));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const days = Math.round(s / 86400);
  if (days < 7) return days === 1 ? "yesterday" : `${days} days ago`;
  return new Date(at).toLocaleDateString([], { month: "short", day: "numeric" });
}
