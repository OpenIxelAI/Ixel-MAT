// Board: Handoff's task board for a project, read and changed through this app's server (which
// asks `handoff api`). Everything a task holds (titles, notes, answers, history) is untrusted: it
// only ever becomes text nodes, never HTML, and pictures a run made are shown from blob: URLs this
// page builds from the server's bytes.

import {
  $, el, icon, ago, plural, copyButton, api, getJSON, postJSON, rememberedProject, rememberProject, PRIVATE_TYPING,
} from "./common.js";
import { renderMarkdown } from "./markdown.js";

const SHOWN_EVERY = 3000;    // an open Board looks for changes this often (the server checks the files first)
const AWAY_EVERY = 20000;    // and this often behind another view, for the rail's dot
const RECENT_KEY = "ixel.board.recent";
const MAX_RECENT = 8;
const MAX_CHOICES = 12;  // projects offered before one is picked
const MAX_TEXT_SHOWN = 200 * 1024;  // an answer bigger than this is listed, not shown

const STATUS_WORDS = {
  open: "Open", claimed: "Claimed", in_progress: "In progress", handed_off: "Handed off", in_review: "In review",
  blocked: "Blocked", done: "Done", cancelled: "Cancelled",
};
const KIND_WORDS = { edit: "Change files", answer: "Answer", review: "Review your changes", image: "Make pictures" };
const KIND_HINTS = {
  edit: "works on its own branch, and nothing is pushed.",
  answer: "writes an answer. Nothing in the project changes.",
  review: "reviews your changes. Nothing in the project changes.",
  image: "makes pictures. Nothing in the project changes.",
};
// The models an edit can run on, by worker: suggestions (any model name can be typed), the one model a run starts on,
// and the chain Claude's edits start with, biggest first, each with its effort: the biggest plans the steps and hands
// each one down to the smallest that can do it well (Handoff's approve args.models, and args.efforts). Fable plans at
// low effort: its planning was three quarters of what a chain cost, and low effort plans well enough for most tasks.
const CLAUDE_MODELS = [
  ["claude-fable-5-1", "Fable 5.1"], ["claude-opus-5-5", "Opus 5.5"], ["claude-sonnet-5-5", "Sonnet 5.5"],
  ["claude-haiku-5-5", "Haiku 5.5"],
];
const WORKER_MODELS = {
  claude: { suggest: CLAUDE_MODELS, one: "claude-opus-5-5",
    chain: [["claude-fable-5-1", "low"], ["claude-opus-5-5", ""], ["claude-haiku-5-5", ""]] },
  codex: { suggest: ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.5", "gpt-5.5-mini"].map((m) => [m, m]),
    one: "gpt-5.5", chain: [] },
};
const MAX_MODELS = 4;  // a planner and up to three models for its steps (Handoff's approvals.MAX_MODELS)
const EFFORT_WORDS = { none: "No thinking", off: "No thinking", minimal: "Minimal", low: "Low", medium: "Medium",
  high: "High", xhigh: "Extra high", max: "Max", ultra: "Ultra", adaptive: "Adaptive" };
// What each worker can reach, as Handoff says (its `workers`' access), in a sentence for the picker
const ACCESS_WORDS = {
  sandbox: "Works in a sandbox on its own branch: it can run your tests, with no network.",
  "edit-only": "Edits files on its own branch only: no commands, no web.",
  files: "Uses file tools only, with no commands, but they can read files outside the project.",
  full: "Runs with its own permissions: it can run commands as you, with the network.",
};
// Model names Handoff's claude and codex take are lowercase; other agents' keep their case (Qwen/Qwen3-Coder)
const LOWERCASE_MODELS = new Set(["claude", "codex"]);
function modelName(id) {
  const found = CLAUDE_MODELS.find(([m]) => m === id);
  return found ? found[1] : id;
}
function dollars(n) {
  return n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;
}
const PRICE_NOTE = "estimated at API prices. On a subscription it counts toward your plan's limits instead.";
// What a cost shows as on a card or at the top of the Board: $0.25, and under a cent, <$0.01
function money(cost) {
  return cost > 0 && cost < 0.01 ? "<$0.01" : `$${cost.toFixed(2)}`;
}
// Each model's short name ("Opus", not "Opus 5.5") and what it cost, dearest first; none without usage
function usageParts(usage) {
  if (!usage || !usage.models) return [];
  return Object.entries(usage.models)
    .map(([id, u]) => [modelName(id).split(" ")[0], u && typeof u.cost === "number" ? u.cost : 0])
    .sort((a, b) => b[1] - a[1])
    .map(([name, cost]) => `${name} ${money(cost)}`);
}

// What an agent does on a run of `kind`, of a pull request when `of` says which (Handoff's run.of, targets)
function kindHint(kind, of) {
  if (of && kind === "review") return `reviews ${of}. Nothing in the project changes.`;
  if (of && kind === "edit") return `works on ${of}, on its own branch, and nothing is pushed.`;
  return KIND_HINTS[kind] || "";
}

const COLUMNS = [
  { id: "you", title: "Waiting on you", empty: "Nothing needs you." },
  { id: "agents", title: "With agents", empty: "No agent has a task." },
  { id: "open", title: "Not assigned", empty: "Every task has someone." },
  { id: "blocked", title: "Blocked", empty: "Nothing is blocked." },
  { id: "done", title: "Done", empty: "Nothing finished yet." },
];
const TERMINAL = new Set(["done", "cancelled"]);
// The word each op that needs typing puts it in, and what the box is called
const TEXT_ARG = { note: "text", status: "reason", review: "note" };
const TEXT_LABEL = { note: "Note", status: "Why it's blocked", review: "What to change" };
const TEXT_NEEDED = { note: true, status: false, review: true };
const FALLBACK_AGENTS = ["claude", "codex"];

const who = (name) => (name === "human" ? "You" : name || "Nobody");
const statusClass = (status) => `pill s-${STATUS_WORDS[status] ? status : "other"}`;

let dot = null;
let visible = false;
let timer = null;
let hello = null;      // Handoff's door: { ok, problem, install, update, project, features }
let project = "";      // the folder the person picked (Handoff's root for it once read)
let board = null;      // the board last read for `project`: { exists, revision, project, counts, tasks }
let failure = null;    // what went wrong with the last look: { message, code }
let notice = "";       // what went wrong starting a board: kept until it's dismissed or tried again
let deleteLeft = "";   // what a delete left (results it couldn't remove, the agent's worktree): kept until dismissed
let looking = null;    // the look under way
let openRef = "";      // the task in the panel
let detail = null;     // its data, from /api/board/task
let taskFailure = "";
let needs = null;      // an action that needs typing or picking first: { action, ref, draft }
let stale = false;     // the task changed while the person was typing: read it again after
let busy = false;      // an action is under way
let actionFailure = "";
let actionNote = "";   // what a finished action says it did (a merge): kept until the next action or task
let agents = null;     // { project, promise } of the agents a task can go to
let pictures = new Map();  // name → { url } for the task in the panel, or { text }
let shownRef = "";         // the task the panel's scroll position belongs to
let panelFocus = "";       // what had focus in the panel, kept while a redraw (or a busy button) loses it
let lastRemembered = "";   // the project this page last remembered: Ask's /handoff may pick another
let prs = null;            // the project's pull requests: { project, data } or { project, error }, read on demand
let prsLooking = null;     // the read under way
let prsBusy = false;       // saving a setting, or starting a review
let prsSaid = "";          // what the last of those said
let prsCopy = "";          // and a command it gave, to copy
let prsDraft = {};         // what's typed in its forms (kind, url, token) and whether they're open, kept across redraws
let found = null;          // git repositories on this computer, offered while no project is open: [{ path, name, board }]
let finding = false;       // asking the server for them
let hint = "";             // what Open or New task said is missing first: "open", "new" or "start"

// ── Rhythm ────────────────────────────────────────────────────────────────

export function start(options) {
  dot = options.dot;
  project = lastRemembered = rememberedProject();
  if (!/Windows/.test(navigator.userAgent)) $("#board-project-input").placeholder = "The project's folder, like ~/code/shop";
  $("#board-project").addEventListener("submit", (e) => {
    e.preventDefault();
    choose($("#board-project-input").value);
  });
  $("#board-new").addEventListener("click", newTask);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && visible && openRef && !document.querySelector("dialog[open]")) {
      e.preventDefault();
      closePanel();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(timer);
    else look().then(schedule);
  });
  fillRecent();
  askHello().then(() => look()).then(schedule);
}

export function shown() {
  visible = true;
  const asked = rememberedProject();  // a /handoff in Ask, for another project, sends you here
  if (asked && asked !== lastRemembered) {
    lastRemembered = asked;
    choose(asked);
  }
  render();
  findProjects();
  if (hello && !hello.ok) askHello().then(() => look());
  else look();
  schedule();
}

export function hidden() {
  visible = false;
  schedule();
}

function schedule() {
  clearTimeout(timer);
  if (document.hidden) return;
  timer = setTimeout(() => look().then(schedule), visible ? SHOWN_EVERY : AWAY_EVERY);
}

async function askHello() {
  try {
    hello = await getJSON("/api/board/hello");
  } catch (e) {
    hello = { ok: false, problem: "internal", message: e.message };
  }
  if (!project && hello.project) project = hello.project;
  render();
  findProjects();
}

// A project to pick is wanted: none is picked yet, or the one picked isn't there (a remembered folder that moved)
function wantsChoices() {
  return Boolean(hello && hello.ok && !board && (!project || (failure && failure.code === "no_project")));
}

// Asked once, the first time the Board is on screen and wants them
async function findProjects() {
  if (found || finding || !visible || !wantsChoices()) return;
  finding = true;
  try {
    found = (await getJSON("/api/board/projects")).projects || [];
  } catch (e) {
    found = [];
  } finally {
    finding = false;
  }
  render();
}

// Reads the board if it changed since the last look; one look at a time
function look() {
  if (looking) return looking;
  if (!hello || !hello.ok || !project) return Promise.resolve();
  const asked = project;
  const since = board && board.project === asked ? board.revision : "";
  looking = (async () => {
    try {
      const data = await getJSON(`/api/board?project=${encodeURIComponent(asked)}` +
        (since ? `&since=${encodeURIComponent(since)}` : ""));
      if (project !== asked) return;  // they picked another project meanwhile
      const recovered = failure !== null;
      failure = null;
      if (data.unchanged) {
        if (recovered) render();
        return;
      }
      board = data;
      if (data.exists) notice = hint = "";  // there's a board now, however it got there
      if (data.project && data.project !== project) project = data.project;
      remember(project);
      if (openRef) await loadTask(openRef);
      render();
      if (data.exists && (!prs || prs.project !== board.project)) lookForPrs();
    } catch (e) {
      if (project !== asked) return;
      failure = { message: e.message, code: e.code };
      if (e.code === "not_installed" || e.code === "outdated") await askHello();
      render();
      findProjects();
    } finally {
      looking = null;
    }
  })();
  return looking;
}

// After a change: the look under way may have started before it
async function refresh() {
  if (looking) await looking;
  await look();
}

function choose(value) {
  let path = value.trim();
  if (path.length > 2 && /^(["']).*\1$/.test(path)) path = path.slice(1, -1).trim();  // Windows' "Copy as path"
  if (!path && !project) { askForProject("open"); return; }
  if (!path || path === project) { look(); return; }
  project = path;
  board = null;
  failure = null;
  notice = "";
  deleteLeft = "";
  hint = "";
  agents = null;
  prs = null;
  prsSaid = "";
  prsCopy = "";
  prsDraft = {};
  closePanel(false);
  render();
  look();
}

// ── Recent projects ───────────────────────────────────────────────────────

function recent() {
  try {
    const list = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    return Array.isArray(list) ? list.filter((p) => typeof p === "string").slice(0, MAX_RECENT) : [];
  } catch (e) {
    return [];
  }
}

function remember(path) {
  rememberProject(path);
  lastRemembered = path;
  const list = [path, ...recent().filter((p) => p !== path)].slice(0, MAX_RECENT);
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch (e) { /* storage is off */ }
  fillRecent();
}

function fillRecent() {
  $("#board-recent").replaceChildren(...recent().map((p) => el("option", { value: p })));
}

// ── The board ─────────────────────────────────────────────────────────────

function column(task) {
  if (TERMINAL.has(task.status)) return "done";
  if (task.assignee === "human" || task.waiting_on === "human" || (task.run && task.run.state === "approved")) return "you";
  if (task.status === "blocked") return "blocked";
  return task.assignee ? "agents" : "open";
}

function render() {
  const waiting = board && board.exists ? board.tasks.filter((t) => column(t) === "you").length : 0;
  if (dot) {
    dot.hidden = !waiting;
    dot.className = "rail-dot you";
    dot.title = waiting ? `${plural(waiting, "task")} waiting on you` : "";
  }
  if (!visible) return;

  const input = $("#board-project-input");
  if (document.activeElement !== input) input.value = project;
  const name = board && board.exists ? board.project.split(/[\\/]/).filter(Boolean).pop() || board.project : "";
  $("#board-where").textContent = name;
  $("#board-where").title = board ? board.project : "";
  // What every run on the board cost, from a Handoff that says (its `spent`)
  const spent = board && board.exists && typeof board.spent === "number" ? board.spent : 0;
  $("#board-spent").hidden = !(spent > 0);
  $("#board-spent").replaceChildren(...(spent > 0 ? [el("span", {}, `Spent about ${money(spent)}`),
    ...usageParts(board.usage).map((part) => el("span", { class: "spent-part" }, ` · ${part}`))] : []));
  $("#board-spent").title = spent > 0 ? `What this board's runs cost, ${PRICE_NOTE}` : "";
  $("#board-new").disabled = !hello || !hello.ok;  // before there's a board, it says what's needed first
  $("#board-project").hidden = !hello || !hello.ok;

  const focused = document.activeElement && document.activeElement.closest(".task-card");
  const focusRef = focused ? focused.dataset.ref : "";
  renderPrs();
  $("#board-main").replaceChildren(...mainView());
  if (focusRef) {
    const again = $(`.task-card[data-ref="${CSS.escape(focusRef)}"]`);
    if (again) again.focus();
  }
}

function mainView() {
  if (!hello) return [waitingLine("Looking for Handoff…")];
  if (!hello.ok) return [handoffMissing()];
  if (!project) return [pickProject()];
  const nodes = [];
  if (failure) nodes.push(el("div", { class: "board-notice error", role: "alert" }, icon("alert"), el("span", {}, failure.message)));
  if (notice) {
    const dismiss = el("button", { type: "button", class: "icon-btn", "aria-label": "Dismiss", title: "Dismiss" }, icon("x"));
    dismiss.addEventListener("click", () => { notice = ""; render(); });
    nodes.push(el("div", { class: "board-notice error", role: "alert" }, icon("alert"), el("span", {}, notice), dismiss));
  }
  if (deleteLeft) {
    const dismiss = el("button", { type: "button", class: "icon-btn", "aria-label": "Dismiss", title: "Dismiss" }, icon("x"));
    dismiss.addEventListener("click", () => { deleteLeft = ""; render(); });
    nodes.push(el("div", { class: "board-notice", role: "status" }, icon("alert"), el("span", {}, deleteLeft), dismiss));
  }
  if (!board && failure && failure.code === "no_project") {
    nodes.push(pickProject());
    return nodes;
  }
  if (!board) {
    if (!failure) nodes.push(waitingLine("Reading the board…"));
    return nodes;
  }
  if (!board.exists) {
    const begin = el("button", { type: "button", class: "btn primary board-start" }, icon("plus"),
      el("span", {}, "Start a board here"));
    begin.addEventListener("click", () => startBoard(begin));
    nodes.push(message("board", "No board here yet",
      `There's no board in ${board.project}. Starting one adds a .handoff folder there, which git ignores.`,
      hintLine(), begin));
    return nodes;
  }
  nodes.push(columns());
  return nodes;
}

// Open with nothing typed, or New task before a project is open: say what's missing, and where it goes
function askForProject(why) {
  hint = why;
  render();
  $("#board-project-input").focus();
}

const HINTS = {
  open: (offered) => `Type a project's folder first${offered ? ", or pick one below" : ""}.`,
  new: (offered) => "Open a project first: its tasks go on its board. Type its folder above" +
    `${offered ? ", or pick one below" : ""}.`,
  start: () => "Start a board here first, then add its tasks.",
};

function hintLine(offered = false) {
  return hint ? el("p", { class: "board-hint", role: "alert" }, HINTS[hint](offered)) : null;
}

// The projects opened here lately, then the git repositories found on this computer
function projectChoices() {
  const byPath = new Map((found || []).map((p) => [p.path, p]));
  const lately = recent();
  const name = (path) => path.split(/[\\/]/).filter(Boolean).pop() || path;
  return [...lately.map((path) => byPath.get(path) || { path, name: name(path), board: false }),
    ...(found || []).filter((p) => !lately.includes(p.path))].filter((p) => p.path !== project).slice(0, MAX_CHOICES);
}

function pickProject() {
  const choices = projectChoices();
  if (!choices.length) {
    return message("folder", "Pick a project",
      "Type the folder of a git repository above, and press Open. Its board lives in that folder (.handoff), " +
      "so each project has its own.", hintLine());
  }
  return message("folder", "Pick a project",
    "Pick one of your projects, or type the folder of a git repository above and press Open. Its board lives " +
    "in that folder (.handoff), so each project has its own.", hintLine(true),
    el("ul", { class: "project-choices" }, choices.map((p) => el("li", {}, choiceButton(p)))));
}

function choiceButton(p) {
  const button = el("button", { type: "button", class: "project-choice", title: p.path },
    icon(p.board ? "board" : "folder"),
    el("span", { class: "project-choice-text" }, el("span", { class: "project-choice-name" }, p.name),
      el("small", {}, p.path)),
    p.board ? el("span", { class: "project-choice-tag" }, "Has a board") : null);
  button.addEventListener("click", () => choose(p.path));
  return button;
}

function waitingLine(text) {
  return el("div", { class: "board-waiting" }, el("span", { class: "spin" }), el("span", {}, text));
}

function message(iconName, title, text, ...extra) {
  return el("div", { class: "board-message" }, el("span", { class: "board-message-icon" }, icon(iconName)),
    el("h2", {}, title), el("p", {}, text), ...extra);
}

function handoffMissing() {
  const again = el("button", { type: "button", class: "btn" }, icon("refresh"), el("span", {}, "Check again"));
  again.addEventListener("click", async () => {
    again.disabled = true;
    hello = null;
    render();
    await askHello();
    await look();
  });
  const line = (text) => el("div", { class: "check-fix" }, el("code", {}, text), copyButton(() => text, "Copy"));
  if (hello.problem === "not_installed") {
    const how = hello.install || {};
    return message("board", "Handoff isn't installed",
      `Handoff gives your agents one board to share: you hand out tasks, they work on them, and what they ` +
      `finish comes back here. To add it, run this in ${how.where || "a terminal"}, then check again:`,
      how.command ? line(how.command) : null, again);
  }
  if (hello.problem === "outdated") {
    return message("board", "Handoff needs an update",
      "This Handoff is older than the Board. Run this, then check again:", line(hello.update || "handoff update"), again);
  }
  return message("alert", "Couldn't reach Handoff", hello.message || "Handoff didn't answer.", again);
}

async function startBoard(button) {
  button.disabled = true;
  notice = "";
  try {
    await postJSON("/api/board/action", { project: board.project, op: "init", args: {} });
  } catch (e) {
    notice = `Couldn't start a board: ${e.message}`;  // not a look's error, so the next look keeps it
  }
  await refresh();
  render();
}

function columns() {
  const groups = Object.fromEntries(COLUMNS.map((c) => [c.id, []]));
  for (const task of board.tasks) groups[column(task)].push(task);
  for (const list of Object.values(groups)) list.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  return el("div", { class: "columns" }, COLUMNS.map((c) => el("section", { class: `column col-${c.id}`, "aria-label": c.title },
    el("h2", {}, el("span", {}, c.title), el("span", { class: "count" }, String(groups[c.id].length))),
    groups[c.id].length
      ? el("ul", { class: "cards" }, groups[c.id].map((t) => el("li", {}, card(t))))
      : el("p", { class: "column-empty" }, c.empty))));
}

function runBadge(run) {
  if (!run) return null;
  if (run.state === "running") return el("span", { class: "run running" }, el("span", { class: "spin" }), `${run.agent} is on it`);
  return el("span", { class: "run approved" }, icon("play"), "Ready to run");
}

function card(t) {
  const button = el("button", { type: "button", class: `task-card${t.ref === openRef ? " open" : ""}`, "data-ref": t.ref },
    el("span", { class: "card-top" },
      el("span", { class: "ref" }, t.ref),
      el("span", { class: statusClass(t.status) }, STATUS_WORDS[t.status] || t.status),
      runBadge(t.run)),
    el("span", { class: "card-title" }, t.title),
    el("span", { class: "card-foot" },
      el("span", { class: `who${t.assignee === "human" ? " you" : ""}` }, who(t.assignee)),
      t.overlaps ? el("span", { class: "overlap", title: "Its files overlap another task's" }, icon("alert"),
        String(t.overlaps)) : null,
      typeof t.cost === "number" && t.cost > 0 ? el("span", { class: "cost-chip",
        title: [...usageParts(t.usage), `What its runs cost, ${PRICE_NOTE}`].join("\n") }, money(t.cost)) : null,
      t.last_event ? el("span", { class: "when", title: t.last_event.at },
        `${who(t.last_event.actor)} ${t.last_event.summary} · ${ago(t.last_event.at)}`) : null));
  button.addEventListener("click", () => openPanel(t.ref));
  return button;
}

// ── Pull requests ─────────────────────────────────────────────────────────
// The open pull requests (merge requests on GitLab) of the project's origin, read when the board opens
// and on Refresh, not every few seconds: the host counts how often it's asked. Review fetches one into
// the project and has an agent review exactly its commits.

const KIND_NAMES = { gitea: "Gitea or Forgejo", gitlab: "GitLab", github: "GitHub Enterprise" };
const TOKEN_CODES = new Set(["needs_token", "refused", "not_found"]);  // a token can fix these
const ADDRESS_CODES = new Set(["unreachable", "invalid", "not_found"]);  // and a wrong address these

function lookForPrs() {
  if (!board || !board.exists) return Promise.resolve();
  const where = board.project;
  if (prsLooking && prsLooking.project === where) return prsLooking.promise;
  const promise = (async () => {
    try {
      const data = await getJSON(`/api/connections?project=${encodeURIComponent(where)}`);
      if (board && board.project === where) prs = { project: where, data };
    } catch (e) {
      if (board && board.project === where) prs = { project: where, error: e.message };
    } finally {
      if (prsLooking && prsLooking.project === where) prsLooking = null;
    }
    renderPrs();
  })();
  prsLooking = { project: where, promise };
  renderPrs();
  return promise;
}

// Drawn only when what it shows changes, so a form being filled in isn't redrawn under the person
let prsDrawn = "";

function renderPrs() {
  const box = $("#board-prs");
  // Shown once there's something to show: not for a project that isn't hosted anywhere (most aren't)
  const local = prs && prs.data && prs.data.problem && prs.data.problem.code === "no_origin";
  const show = visible && hello && hello.ok && board && board.exists && prs && prs.project === board.project && !local;
  box.hidden = !show;
  if (!show) { prsDrawn = ""; return; }
  const state = JSON.stringify([board.project, prs, Boolean(prsLooking), prsBusy, prsSaid, prsCopy]);
  if (state === prsDrawn) return;
  const keep = box.contains(document.activeElement) ? document.activeElement.dataset.key : "";
  prsDrawn = state;
  box.replaceChildren(...prsView());
  if (keep) {
    const again = box.querySelector(`[data-key="${CSS.escape(keep)}"]`);
    if (again) again.focus();
  }
}

function prsHeader(text) {
  const again = el("button", { type: "button", class: "icon-btn", title: "Look again", "aria-label": "Look again",
    disabled: Boolean(prsLooking) || prsBusy, "data-key": "prs-refresh" }, icon("refresh"));
  again.addEventListener("click", () => { prsSaid = ""; prsCopy = ""; lookForPrs(); });
  return el("div", { class: "prs-head" }, icon("pull"), el("h2", { id: "board-prs-title" }, "Pull requests"),
    el("span", { class: "prs-where" }, text), el("span", { class: "spacer" }), again);
}

function prsView() {
  if (prsLooking && !prs.data) return [prsHeader(""), el("p", { class: "prs-line" }, el("span", { class: "spin" }), "Looking for pull requests…")];
  if (prs.error) return [prsHeader(""), el("p", { class: "prs-line error" }, prs.error)];
  const { origin, host, problem, prs: list } = prs.data;
  const where = origin ? `${origin.path} on ${origin.host}` : "";
  const nodes = [prsHeader(where)];
  if (prsSaid) nodes.push(el("p", { class: "prs-line said", role: "status" }, prsSaid));
  if (prsSaid && prsCopy) nodes.push(el("div", { class: "check-fix" }, el("code", {}, prsCopy), copyButton(() => prsCopy, "Copy")));
  if (problem) {
    nodes.push(el("p", { class: `prs-line${problem.code === "no_origin" ? "" : " error"}` }, problem.message));
    // Whatever went wrong, what fixes it is here: the host's kind and address, or its token
    if (!host && origin) nodes.push(hostForm(origin, null));
    if (host) nodes.push(prsSettings(origin, host, problem.code));
    return nodes;
  }
  if (!list.length) {
    nodes.push(el("p", { class: "prs-line" }, `No open ${host.kind === "gitlab" ? "merge" : "pull"} requests.`));
  } else {
    nodes.push(el("ul", { class: "prs" }, list.map((pr) => el("li", {}, prRow(pr, host)))));
  }
  nodes.push(prsSettings(origin, host));
  return nodes;
}

function prRow(pr, host) {
  const mark = host.kind === "gitlab" ? "!" : "#";
  const review = el("button", { type: "button", class: "btn", disabled: prsBusy, "data-key": `pr-review:${pr.number}` },
    el("span", {}, "Review…"));
  review.addEventListener("click", () => reviewPr(pr, host));
  const fix = el("button", { type: "button", class: "btn", disabled: prsBusy, "data-key": `pr-fix:${pr.number}` },
    el("span", {}, "Fix…"));
  fix.addEventListener("click", () => fixPr(pr, host));
  const meta = [pr.author, pr.head && pr.base ? `${pr.head} → ${pr.base}` : pr.base ? `into ${pr.base}` : "",
    pr.draft ? "draft" : "", pr.updated ? ago(pr.updated) : ""].filter(Boolean).join(" · ");
  return el("div", { class: "pr" }, el("span", { class: "pr-number" }, `${mark}${pr.number}`),
    el("span", { class: "pr-text" }, el("span", { class: "pr-title" }, pr.title || "(no title)"), el("small", {}, meta)),
    el("span", { class: "pr-buttons" }, review, fix));
}

// Under the list: the token (saved or not), and for a host that was set, what was set
function prsSettings(origin, host, code) {
  const details = el("details", { class: "prs-settings", "data-key": "prs-settings" }, el("summary", {}, host.token === "none"
    ? `No token saved for ${host.label}` : `A token for ${host.label} is ${host.token === "system" ? "set outside Ixel" : "saved"}`));
  details.open = prsDraft.open !== undefined ? prsDraft.open : TOKEN_CODES.has(code) || ADDRESS_CODES.has(code);
  details.addEventListener("toggle", () => { prsDraft.open = details.open; });
  details.append(tokenForm(host));
  if (host.set || ADDRESS_CODES.has(code)) details.append(hostForm(origin, host));
  return details;
}

function hostForm(origin, host) {
  const kind = el("select", { id: "prs-kind", "data-key": "prs-kind" },
    ...Object.entries(KIND_NAMES).map(([value, label]) => el("option", { value }, label)));
  kind.value = prsDraft.kind || (host && KIND_NAMES[host.kind] ? host.kind : "gitea");
  kind.addEventListener("change", () => { prsDraft.kind = kind.value; });
  const url = el("input", { type: "url", id: "prs-url", "data-key": "prs-url", spellcheck: "false", autocomplete: "off",
    placeholder: `https://${origin.host}` });
  url.value = prsDraft.url !== undefined ? prsDraft.url : host ? host.web : origin.web;
  url.addEventListener("input", () => { prsDraft.url = url.value; });
  const form = el("form", { class: "prs-form" },
    el("p", {}, `Which kind of host is ${origin.host}, and where are its web pages? A token is only ever sent there.`),
    el("div", { class: "prs-fields" },
      el("label", { for: "prs-kind" }, "Kind"), kind,
      el("label", { for: "prs-url" }, "Address"), url),
    el("button", { type: "submit", class: "btn primary", disabled: prsBusy, "data-key": "prs-host-save" }, "Save"));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    savePrs("/api/connections/host", { kind: kind.value, url: url.value.trim() }, "Saved.", () => {
      delete prsDraft.kind;
      delete prsDraft.url;
    });
  });
  return form;
}

function tokenForm(host) {
  const value = el("input", { type: "password", id: "prs-token", "data-key": "prs-token", autocomplete: "off",
    spellcheck: "false", placeholder: host.token === "none" ? "Paste a token" : "Paste a new token to replace it" });
  value.value = prsDraft.token || "";
  value.addEventListener("input", () => { prsDraft.token = value.value; });
  const form = el("form", { class: "prs-form" },
    el("p", {}, `A token lets Ixel read pull requests in a private repository. Read-only access is enough: Ixel ` +
      `never writes to ${host.label}. It's kept with your other keys, and only ever sent to ${host.web}.`),
    el("div", { class: "prs-fields" }, el("label", { for: "prs-token" }, "Token"), value),
    el("div", { class: "prs-buttons" },
      host.token === "file" ? removeTokenButton() : null,
      el("button", { type: "submit", class: "btn primary", disabled: prsBusy, "data-key": "prs-token-save" }, "Save token")));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (value.value.trim()) savePrs("/api/connections/token", { value: value.value }, "", () => { delete prsDraft.token; });
  });
  return form;
}

function removeTokenButton() {
  const button = el("button", { type: "button", class: "btn", disabled: prsBusy, "data-key": "prs-token-remove" }, "Remove token");
  button.addEventListener("click", () => savePrs("/api/connections/token", { remove: true }, ""));
  return button;
}

// A setting saved for the project it was typed for, even if another one is opened meanwhile. What was typed
// is forgotten only once it's saved (onSaved), so a refused one is there to fix
async function savePrs(path, body, said, onSaved) {
  if (prsBusy || !board) return;
  const where = board.project;
  const here = () => board && board.project === where;
  prsBusy = true;
  prsSaid = "";
  prsCopy = "";
  renderPrs();
  try {
    const reply = await postJSON(path, { project: where, ...body });
    if (here()) {
      prsSaid = reply.message || said;
      if (onSaved) onSaved();
    }
  } catch (e) {
    if (here()) prsSaid = e.message;
  } finally {
    prsBusy = false;
  }
  if (here()) await lookForPrs();
  else renderPrs();
}

async function reviewPr(pr, host) {
  if (prsBusy || !board) return;
  const where = board.project;  // (another project may be opened while it's fetched)
  const mark = host.kind === "gitlab" ? `merge request !${pr.number}` : `pull request #${pr.number}`;
  const list = (await getAgents()).filter((a) => !a.kinds || a.kinds.includes("review"));
  if (!list.length) {
    prsCopy = "";
    prsSaid = "None of your agents can review here. Add a model with ixel setup.";
    renderPrs();
    return;
  }
  const select = el("select", { id: "pr-agent" }, list.map((a) =>
    el("option", { value: a.name }, a.label && a.label !== a.name ? `${a.label} (${a.name})` : a.name)));
  select.value = list.some((a) => a.name === "codex") ? "codex" : list[0].name;
  const answer = showDialog(el("form", { method: "dialog" },
    el("h2", {}, `Review ${mark}?`),
    el("p", {}, pr.title || ""),
    el("p", {}, `Ixel fetches it from origin into this project, without changing your files or branches, and the ` +
      "agent you pick reviews exactly its commits now. Its review comes back to this board."),
    el("div", { class: "field" }, el("label", { for: "pr-agent" }, "Reviewed by"), select),
    dialogButtons("Fetch and review", false)));
  select.focus();
  if ((await answer) !== "yes" || !board || board.project !== where) return;
  prsBusy = true;
  prsSaid = `Fetching ${mark}…`;
  prsCopy = "";
  renderPrs();
  let started = null;
  let said = "";
  try {
    started = await postJSON("/api/connections/review", { project: where, number: pr.number, agent: select.value });
    said = started.problem
      ? `${started.task} is on the board, approved for ${select.value}, but couldn't start: ${started.problem}`
      : `${started.task}: ${select.value} is reviewing ${started.label}.`;
  } catch (e) {
    said = `Couldn't start the review: ${e.message}`;
  } finally {
    prsBusy = false;
  }
  if (!board || board.project !== where) { renderPrs(); return; }
  prsSaid = said;
  renderPrs();
  await refresh();
  if (started && started.task && board && board.project === where) openPanel(started.task);
}

// What the server calls it (connections_api._label), so its review tasks can be found by their titles
function prLabel(pr, host) {
  const what = host.kind === "gitlab" ? `merge request !${pr.number}` : `pull request #${pr.number}`;
  return `${what} (${pr.head ? `${pr.head} into ${pr.base}` : `into ${pr.base}`})`;
}

const MAX_FIX_CHARS = 16000;  // (the server counts it in bytes, the same 16 KB)

// The newest review of this pull request on the board that left an answer, to start the fix from
async function lastReview(pr, host, where) {
  const title = `Review ${prLabel(pr, host)}:`;
  const reviews = (board.tasks || []).filter((t) => typeof t.title === "string" && t.title.startsWith(title))
    .sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
  for (const t of reviews.slice(0, 3)) {
    try {
      const res = await api(`/api/board/output?project=${encodeURIComponent(where)}&task=${encodeURIComponent(t.ref)}` +
        "&name=answer.md");
      if (res.ok) return { ref: t.ref, text: (await res.text()).trim() };
    } catch { /* no answer yet: an older one, or none */ }
  }
  return null;
}

async function fixPr(pr, host) {
  if (prsBusy || !board) return;
  const where = board.project;
  const mark = host.kind === "gitlab" ? `merge request !${pr.number}` : `pull request #${pr.number}`;
  const list = (await getAgents()).filter((a) => (a.kinds ? a.kinds.includes("edit") : FALLBACK_AGENTS.includes(a.name)));
  if (!list.length) {
    prsCopy = "";
    prsSaid = "Only Claude Code and Codex can change files, and neither can here. `handoff agents` says why.";
    renderPrs();
    return;
  }
  const select = el("select", { id: "pr-fixer" }, list.map((a) =>
    el("option", { value: a.name }, a.label && a.label !== a.name ? `${a.label} (${a.name})` : a.name)));
  select.value = list.some((a) => a.name === "claude") ? "claude" : list[0].name;
  const text = el("textarea", { id: "pr-fix-text", rows: "8", maxlength: String(MAX_FIX_CHARS), ...PRIVATE_TYPING,
    placeholder: "What should change? For example: handle a declined card." });
  const from = el("small", {}, "Looking for a review of it on the board…");
  const problem = el("div", { class: "dialog-problem", role: "alert" });
  const form = el("form", { method: "dialog" },
    el("h2", {}, `Fix ${mark}?`),
    el("p", {}, pr.title || ""),
    el("p", {}, "Ixel fetches it from origin into this project, and the agent you pick changes it on a branch of " +
      "its own that starts from its last commit. Nothing is pushed: when it's done, the board shows the line " +
      "that adds the work to the pull request."),
    el("div", { class: "field" }, el("label", { for: "pr-fixer" }, "Changed by"), select),
    el("div", { class: "field" }, el("label", { for: "pr-fix-text" }, "What to change"), text, from),
    problem,
    dialogButtons("Fetch and fix", false));
  let started = null;
  let pending = null;  // the request, once "Fetch and fix" was pressed
  let closed = false;  // (Escape closes the dialog even then: the answer still comes, to the strip)
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const buttons = [...form.querySelectorAll("button")];
    if (buttons.some((b) => b.disabled) || pending) return;
    if (!text.value.trim()) {
      problem.replaceChildren(el("div", { class: "board-notice error" }, icon("alert"), el("span", {}, "Say what to change.")));
      text.focus();
      return;
    }
    buttons.forEach((b) => { b.disabled = true; });
    problem.replaceChildren(el("p", { class: "prs-line" }, el("span", { class: "spin" }), `Fetching ${mark}…`));
    prsBusy = true;  // nothing else starts from the list meanwhile
    renderPrs();
    pending = postJSON("/api/connections/fix", { project: where, number: pr.number, agent: select.value, text: text.value });
    try {
      started = await pending;
      if (!closed) $("#board-dialog").close("yes");  // only this dialog, never one opened since
    } catch (err) {
      pending = null;
      if (!closed) {
        problem.replaceChildren(el("div", { class: "board-notice error" }, icon("alert"),
          el("span", {}, `Couldn't start it: ${err.message}`)));
        buttons.forEach((b) => { b.disabled = false; });
      }
    } finally {
      prsBusy = false;
      renderPrs();
    }
  });
  const answer = showDialog(form).then((value) => { closed = true; return value; });
  text.focus();
  lastReview(pr, host, where).then((found) => {
    if (!found || !found.text) {
      from.textContent = "Say what to change, or review it first and the review starts this off.";
      return;
    }
    from.textContent = `From ${found.ref}'s review. Change it as you like: the agent gets what's here.`;
    if (!text.value.trim()) text.value = `Fix what this review found:\n\n${found.text}`.slice(0, MAX_FIX_CHARS);
  });
  await answer;
  if (pending) {
    try {
      await pending;
    } catch (err) {
      if (board && board.project === where) {
        prsCopy = "";
        prsSaid = `Couldn't start the fix: ${err.message}`;
        renderPrs();
      }
      return;
    }
  }
  if (!started || !board || board.project !== where) return;
  prsCopy = started.problem ? "" : started.push || "";
  prsSaid = started.problem
    ? `${started.task} is on the board, approved for ${select.value}, but couldn't start: ${started.problem}`
    : started.push
      ? `${started.task}: ${select.value} is fixing ${started.label}. When it's done, add it to the pull request with:`
      : `${started.task}: ${select.value} is fixing ${started.label}. It came from a fork, so pushing to origin won't update it; the task says what to do.`;
  renderPrs();
  await refresh();
  if (started.task && board && board.project === where) openPanel(started.task);
}

// ── One task ──────────────────────────────────────────────────────────────

function forgetPictures() {
  for (const p of pictures.values()) if (p.url) URL.revokeObjectURL(p.url);
  pictures = new Map();
}

async function openPanel(ref) {
  if (openRef !== ref) forgetPictures();
  openRef = ref;
  detail = null;
  taskFailure = "";
  actionFailure = "";
  actionNote = "";
  needs = null;
  render();
  renderPanel();
  await loadTask(ref);
  const title = $("#task-title");
  if (title && openRef === ref) title.focus();
}

function closePanel(focusCard = true) {
  const ref = openRef;
  openRef = "";
  detail = null;
  needs = null;
  forgetPictures();
  $("#task-panel").hidden = true;
  $("#task-panel").replaceChildren();
  $("#view-board").classList.remove("panel-open");
  if (visible) render();
  const back = ref && $(`.task-card[data-ref="${CSS.escape(ref)}"]`);
  if (focusCard) (back || $("#board-title")).focus();
}

// Reads the task in the panel again; draws it only if something in it changed (or `force`)
async function loadTask(ref, force = false) {
  if (needs) { stale = true; return; }  // not under the person's typing: after it
  const before = JSON.stringify([detail, taskFailure]);
  try {
    const data = await getJSON(`/api/board/task?project=${encodeURIComponent(board ? board.project : project)}` +
      `&task=${encodeURIComponent(ref)}`);
    if (openRef !== ref) return;
    detail = data;
    taskFailure = "";
  } catch (e) {
    if (openRef !== ref) return;
    taskFailure = e.code === "not_found" ? `${ref} isn't on the board any more.` : e.message;
  }
  stale = false;
  if (force || needs || JSON.stringify([detail, taskFailure]) !== before) renderPanel();
}

function focusKey(node) {
  return (node && node.dataset && node.dataset.key) || "";
}

function renderPanel() {
  const panel = $("#task-panel");
  $("#view-board").classList.toggle("panel-open", Boolean(openRef));
  if (!openRef) { panel.hidden = true; return; }
  panel.hidden = false;
  // Where the person was: the same scroll and the same control after the redraw, for the same task
  const active = document.activeElement;
  const sameTask = shownRef === openRef;
  if (!sameTask) panelFocus = "";
  else if (active && panel.contains(active)) panelFocus = focusKey(active);
  else if (active && active !== document.body) panelFocus = "";
  const old = $(".panel-scroll", panel);
  const top = sameTask && old ? old.scrollTop : 0;
  shownRef = openRef;
  const t = detail && detail.task;
  const close = el("button", { type: "button", class: "icon-btn", "aria-label": "Close", title: "Close (Esc)",
    "data-key": "close" }, icon("x"));
  close.addEventListener("click", () => closePanel());
  const head = el("header", { class: "panel-head" }, el("span", { class: "ref" }, openRef),
    t ? el("span", { class: statusClass(t.status) }, STATUS_WORDS[t.status] || t.status) : null,
    t ? runBadge(t.run) : null, el("span", { class: "spacer" }), close);
  const scroll = el("div", { class: "panel-scroll" });
  if (!detail) {
    scroll.append(taskFailure ? el("div", { class: "board-notice error" }, icon("alert"), el("span", {}, taskFailure))
      : waitingLine("Reading the task…"));
  } else {
    scroll.append(...taskView(detail));
  }
  panel.replaceChildren(head, scroll);
  scroll.scrollTop = top;
  if (panelFocus && (!document.activeElement || document.activeElement === document.body)) {
    const again = $(`[data-key="${CSS.escape(panelFocus)}"]`, panel);
    if (again && !again.disabled) again.focus({ preventScroll: true });
    else if (!busy && !again) {
      const title = $("#task-title");  // what it was on went away (the action is done): stay in the panel
      if (title) title.focus({ preventScroll: true });
      panelFocus = title ? "title" : "";
    }
  }
}

function taskView(data) {
  const t = data.task;
  const facts = [["Assigned to", who(t.assignee)]];
  if (t.waiting_on) facts.push(["Waiting on", who(t.waiting_on)]);
  if (t.branch) facts.push(["Branch", el("code", {}, t.branch)]);
  if (t.usage && t.usage.models) {
    facts.push(["Used", `about ${dollars(t.cost || 0)} · ${Object.keys(t.usage.models).map(modelName).join(", ")}`]);
  }
  if (t.parent) facts.push(["Part of", t.parent]);
  facts.push(["Added", `by ${t.created_by === "human" ? "you" : t.created_by} ${ago(t.created_at)}`]);
  const nodes = [
    el("h2", { class: "task-title", id: "task-title", tabindex: "-1", "data-key": "title" }, t.title),
    el("dl", { class: "facts" }, facts.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v)))),
  ];
  if (t.run) {
    nodes.push(el("p", { class: "run-line" }, t.run.state === "running"
      ? `${t.run.agent} is working on it now (${(KIND_WORDS[t.run.kind] || t.run.kind).toLowerCase()}).`
      : `Approved: ${t.run.agent} ${kindHint(t.run.kind, t.run.of)} Run it now starts it.`));
  }
  nodes.push(actionBar(data));
  if (needs && needs.ref === t.ref) nodes.push(needsForm(needs, t));
  if (actionFailure) nodes.push(el("div", { class: "board-notice error", role: "alert" }, icon("alert"), el("span", { class: "notice-lines" }, actionFailure)));
  if (actionNote) nodes.push(el("div", { class: "board-notice", role: "status" }, el("span", {}, actionNote)));
  if (t.body) nodes.push(section("Details", el("div", { class: "task-body" }, t.body)));
  if (t.acceptance && t.acceptance.length) {
    nodes.push(section("Done when", el("ul", { class: "acceptance" }, t.acceptance.map((a) => el("li", {}, a)))));
  }
  if (data.outputs && data.outputs.length) nodes.push(section("What the run left", outputs(t.ref, data.outputs)));
  if (t.usage && t.usage.models) nodes.push(section("What it used", usageTable(t.usage, t.cost || 0)));
  if (data.overlaps && data.overlaps.length) {
    nodes.push(section("Overlaps", el("ul", { class: "plain warn" }, data.overlaps.map((o) => el("li", {}, o)))));
  }
  if (data.claims && data.claims.length) {
    nodes.push(section("Files it's working on", el("ul", { class: "plain mono" }, data.claims.map((c) => el("li", {}, c)))));
  }
  if (data.children && data.children.length) {
    nodes.push(section("Parts of it", el("ul", { class: "cards" }, data.children.map((c) => el("li", {}, card(c))))));
  }
  nodes.push(section("History", history(data.events || [])));
  return nodes;
}

// Every run's tokens and cost, model by model, as Claude Code reported them (at API prices: on a subscription it's
// what the work would have cost, and it counts toward the plan's limits instead)
function usageTable(usage, total) {
  const rows = Object.entries(usage.models).map(([id, u]) => el("tr", {},
    el("th", { scope: "row", title: id }, modelName(id)), el("td", {}, tokens(u.in + u.cache_read + u.cache_write)),
    el("td", {}, tokens(u.out)), el("td", {}, dollars(u.cost))));
  if (rows.length > 1) {
    rows.push(el("tr", { class: "total" }, el("th", { scope: "row" }, "Total"), el("td", {}), el("td", {}),
      el("td", {}, dollars(total))));
  }
  return el("div", {},
    el("table", { class: "usage" },
      el("thead", {}, el("tr", {}, el("th", {}, "Model"), el("th", {}, "Read"), el("th", {}, "Wrote"), el("th", {}, "Cost"))),
      el("tbody", {}, rows)),
    el("p", { class: "usage-note" }, "Estimated at API prices. On a subscription it counts toward your plan's limits instead."));
}

function tokens(n) {
  if (n < 1000) return String(n);
  if (n < 1000000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1000000).toFixed(1)}M`;
}

function section(title, ...children) {
  return el("section", { class: "panel-section" }, el("h3", {}, title), ...children);
}

function history(events) {
  return el("ol", { class: "history" }, events.slice().reverse().map((e) => el("li", {},
    el("div", { class: "event-head" }, el("b", {}, who(e.actor)), el("span", {}, e.summary),
      el("span", { class: "when", title: e.at }, ago(e.at))),
    e.text ? el("div", { class: "event-text" }, e.text) : null,
    e.detail ? el("pre", { class: "event-detail" }, e.detail) : null,
    e.panel ? el("details", { class: "event-panel" }, el("summary", {}, "The panel's review"), el("pre", {}, e.panel)) : null)));
}

// ── Files a run left ──────────────────────────────────────────────────────

const PICTURE = /\.(png|jpe?g|webp)$/i;
const TEXT = /\.(md|txt)$/i;

function size(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function outputs(ref, list) {
  return el("ul", { class: "outputs" }, list.map((o) => {
    const body = el("div", { class: "output-body" });
    const item = el("li", { class: "output" },
      el("div", { class: "output-head" }, icon("file"), el("span", { class: "name" }, o.name), el("span", { class: "size" }, size(o.size))),
      body);
    if (PICTURE.test(o.name) || (TEXT.test(o.name) && o.size <= MAX_TEXT_SHOWN)) showOutput(ref, o, body);
    return item;
  }));
}

function fillOutput(body, name, content) {
  if (content.url) body.replaceChildren(el("img", { src: content.url, alt: `${name}, from the run`, class: "output-picture" }));
  else if (/\.md$/i.test(name)) body.replaceChildren(el("div", { class: "md" }, renderMarkdown(content.text)));
  else body.replaceChildren(el("pre", { class: "output-text" }, content.text));
}

async function showOutput(ref, o, body) {
  const key = `${o.name}:${o.size}`;
  const known = pictures.get(key);
  if (known) { fillOutput(body, o.name, known); return; }
  body.replaceChildren(waitingLine("Opening…"));
  try {
    const res = await api(`/api/board/output?project=${encodeURIComponent(board.project)}&task=${encodeURIComponent(ref)}` +
      `&name=${encodeURIComponent(o.name)}`);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `Couldn't open it (${res.status})`);
    let content;
    if (PICTURE.test(o.name)) {
      const blob = await res.blob();
      if (openRef !== ref || !/^image\/(png|jpeg|webp)$/.test(blob.type)) return;
      content = { url: URL.createObjectURL(blob) };
    } else {
      content = { text: await res.text() };
      if (openRef !== ref) return;
    }
    pictures.set(key, content);
    fillOutput(body, o.name, content);
  } catch (e) {
    body.replaceChildren(el("p", { class: "output-error" }, e.message));
  }
}

// ── Doing things ──────────────────────────────────────────────────────────

function isDanger(a) {
  return a.op === "delete" || (a.op === "status" && a.args && a.args.to === "cancelled");
}

function actionBar(data) {
  const bar = el("div", { class: "actions" });
  for (const a of data.actions || []) {
    const button = el("button", { type: "button", class: `btn${a.primary ? " primary" : ""}${isDanger(a) ? " danger" : ""}`,
      disabled: busy, "data-key": `action:${a.op}:${JSON.stringify(a.args || {})}` },
      a.op === "approve" || a.op === "run.start" ? icon("play") : null, el("span", {}, a.label));
    button.addEventListener("click", () => pick(a, data.task));
    bar.append(button);
  }
  return bar;
}

function pick(a, t) {
  if (busy) return;
  actionFailure = "";
  actionNote = "";
  if (a.needs) {
    needs = { action: a, ref: t.ref, draft: undefined };
    renderPanel();
    const field = $("#task-panel .needs [data-first]");
    if (field) field.focus();
    return;
  }
  confirmThen(a, t, a.args || {});
}

function closeNeeds() {
  needs = null;
  if (stale) loadTask(openRef);
  else renderPanel();
}

// How an edit runs: on the worker's own model, on one model, or as a chain of up to four, biggest first, each at an
// effort the worker takes (and, where Ixel knows them, the model takes). Auto, when this Handoff can pick a crew,
// asks it once for this task and shows that crew, still editable; approve sends those rows, never a bare "auto",
// so what was seen is what is sealed. What's picked lives in `asked.run` (not `asked.draft`, the kind), so a redraw
// keeps it. Returns the row and args(), the approve args it makes ({} for the worker's own model, as it is).
function runPicker(asked, agent, worker, t) {
  const names = WORKER_MODELS[agent] || { suggest: [], one: worker.default_model || "", chain: [] };
  const tidy = (model) => (LOWERCASE_MODELS.has(agent) ? (model || "").trim().toLowerCase() : (model || "").trim());
  const state = asked.run || (asked.run = { mode: "own", rows: [{ model: "", effort: "" }], auto: false });
  if (state.auto === undefined) state.auto = false;  // a choice remembered from before Auto existed
  const levels = new Map();  // model -> the levels it takes, once the server has said
  const asking = new Map();  // model -> the server's answer on its way
  const listId = `needs-models-${agent}`;
  const rows = el("div", { class: "pick-rows" });
  const modeLabel = el("label", { for: "needs-run-mode" }, "Which model?");
  const mode = el("select", { id: "needs-run-mode", "data-key": "needs-run-mode" },
    el("option", { value: "own" }, worker.local ? "The server's first model" : `${agent}'s own model`),
    el("option", { value: "one" }, "One model"),
    worker.chain ? el("option", { value: "chain" }, "A chain of models, biggest first") : null);
  mode.value = state.mode;
  const hint = el("small", {});
  // Only a Handoff that lists auto-crew, and a worker that runs a chain: otherwise the form is as it was
  const autoOn = !!(hello && Array.isArray(hello.features) && hello.features.includes("auto-crew") && worker.chain);
  const usingCrew = () => autoOn && state.auto;
  let crewRows = null;
  let crewStatus = null;
  let crewActions = null;
  let crewHint = null;

  const allowed = (model) => {
    const takes = levels.get(tidy(model));
    return takes ? worker.efforts.filter((l) => takes.includes(l)) : worker.efforts;
  };
  // "agent:model" (grok:grok-4.7) is that agent's; a bare name is this worker's. Claude and Codex lowercase models.
  const knownAgent = (who) => {
    const lower = who.toLowerCase();
    if (LOWERCASE_MODELS.has(lower)) return lower;
    const workers = (t && t.workers) || {};
    if (workers[who]) return who;
    if (workers[lower]) return lower;
    return who;
  };
  const modelParts = (raw) => {
    const text = (raw || "").trim();
    const colon = text.indexOf(":");
    if (colon > 0 && colon < text.length - 1) {
      const who = text.slice(0, colon).trim();
      const name = text.slice(colon + 1).trim();
      if (who && name) {
        const whoId = knownAgent(who);
        const key = LOWERCASE_MODELS.has(whoId) ? name.toLowerCase() : name;
        return { agent: whoId, key, prefixed: true };
      }
    }
    return { agent, key: tidy(text), prefixed: false };
  };
  const tidyCrew = (raw) => {
    const parts = modelParts(raw);
    return parts.prefixed ? `${parts.agent}:${parts.key}` : parts.key;
  };
  const effortsOf = (who) => {
    const known = t && t.workers && t.workers[who];
    if (known && Array.isArray(known.efforts)) return known.efforts;
    return Array.isArray(worker.efforts) ? worker.efforts : [];
  };
  const crewAllowed = (model) => {
    const parts = modelParts(model);
    const takes = levels.get(`${parts.agent}\n${parts.key}`);
    const efforts = effortsOf(parts.agent);
    return takes ? efforts.filter((l) => takes.includes(l)) : efforts;
  };
  const effortSelect = (row, i, prefix = "needs-effort") => {
    const crew = prefix === "needs-crew-effort";
    const choices = crew ? crewAllowed(row.model) : allowed(row.model);
    const select = el("select", {
      "aria-label": `Effort for ${row.model || "its own model"}`, "data-key": `${prefix}-${i}`,
    }, [["", "Default effort"], ...choices.map((l) => [l, `${EFFORT_WORDS[l] || l} effort`])]
      .map(([value, text]) => el("option", { value }, text)));
    if (![...select.options].some((o) => o.value === row.effort)) row.effort = "";
    select.value = row.effort;
    select.disabled = !choices.length;
    if (select.disabled) select.title = `${row.model} has no effort setting, so it thinks at its default`;
    select.addEventListener("change", () => { row.effort = select.value; });
    return select;
  };
  const ask = (row, i, prefix = "needs-effort") => {  // the levels this model takes, where Ixel knows its company's
    const crew = prefix === "needs-crew-effort";
    const parts = crew ? modelParts(row.model) : { agent, key: tidy(row.model) };
    const model = parts.key;
    const who = parts.agent;
    const cacheKey = crew ? `${who}\n${model}` : model;
    if (!model || levels.has(cacheKey) || !LOWERCASE_MODELS.has(who)) return;  // (other workers' levels are their own)
    if (!asking.has(cacheKey)) {
      const query = `agent=${encodeURIComponent(who)}&model=${encodeURIComponent(model)}`;
      asking.set(cacheKey, getJSON(`/api/board/levels?${query}`)
        .then((found) => { if (Array.isArray(found.levels)) levels.set(cacheKey, found.levels); })
        .catch(() => { /* the worker's own levels stand */ })
        .finally(() => asking.delete(cacheKey)));
    }
    const seen = row.model;  // a crew reply is for the name asked, not one typed since
    const holder = crew ? crewRows : rows;
    asking.get(cacheKey).then(() => {
      const old = holder && holder.querySelector(`[data-key="${prefix}-${i}"]`);
      const list = crew ? ((state.crew && state.crew.rows) || []) : state.rows;
      const same = (list[i] || {}).model === (crew ? seen : row.model);
      if (levels.has(cacheKey) && old && same) old.replaceWith(effortSelect(row, i, prefix));
    });
  };
  const modelBox = (row, i, prefix, label) => {
    const box = el("input", { type: "text", list: listId, value: row.model, spellcheck: "false", autocomplete: "off",
      maxlength: "80", placeholder: "a model name", "aria-label": label, "data-key": `${prefix}-${i}` });
    box.addEventListener("input", () => { row.model = box.value; });
    box.addEventListener("change", () => {
      row.model = box.value;
      const effortPrefix = prefix === "needs-crew-model" ? "needs-crew-effort" : "needs-effort";
      ask(row, i, effortPrefix);
      // A crew model's effort list follows its agent, even when that agent has no levels lookup
      if (prefix === "needs-crew-model" && crewRows) {
        const old = crewRows.querySelector(`[data-key="${effortPrefix}-${i}"]`);
        if (old) old.replaceWith(effortSelect(row, i, effortPrefix));
      }
    });
    return box;
  };
  const draw = () => {
    const changed = state.mode !== mode.value;  // (a redraw keeps what was typed, a blank model too)
    state.mode = mode.value;
    if (state.mode === "own") state.rows = [{ model: "", effort: state.rows[0] && !state.rows[0].model ? state.rows[0].effort : "" }];
    if (state.mode === "one" && (state.rows.length !== 1 || (changed && !state.rows[0].model))) {
      state.rows = [{ model: names.one, effort: "" }];
    }
    if (state.mode === "chain" && state.rows.length < 2) state.rows = names.chain.map(([model, effort]) => ({ model, effort }));
    rows.replaceChildren(...state.rows.map((row, i) => {
      const parts = [];
      if (state.mode !== "own") {
        const role = state.mode === "chain" ? (i === 0 ? "Plans" : `Step model ${i}`) : "Model";
        parts.push(el("span", { class: "pick-role" }, role));
        const label = state.mode === "chain" && i === 0 ? "Planning model" : "Model";
        parts.push(modelBox(row, i, "needs-model", label));
      }
      parts.push(effortSelect(row, i));
      if (state.mode === "chain" && state.rows.length > 2) {
        const remove = el("button", { type: "button", class: "btn icon-only", title: "Take this model out",
          "aria-label": `Take ${row.model || "this model"} out`, "data-key": `needs-remove-${i}` }, "×");
        remove.addEventListener("click", () => { state.rows.splice(i, 1); draw(); });
        parts.push(remove);
      }
      ask(row, i);
      return el("div", { class: "pick-row" }, parts);
    }));
    if (state.mode === "chain" && state.rows.length < MAX_MODELS) {
      const add = el("button", { type: "button", class: "btn", "data-key": "needs-add-model" }, "Add a model");
      add.addEventListener("click", () => { state.rows.push({ model: "", effort: "" }); draw(); });
      rows.append(add);
    }
    hint.textContent = state.mode === "chain"
      ? "The first model plans and gives each step to the smallest of the others that can do it. The task shows what each model used."
      : "Higher effort thinks longer and uses more. The task shows what each model used.";
  };
  mode.addEventListener("change", draw);
  draw();
  const list = el("datalist", { id: listId }, names.suggest.map(([id, name]) => el("option", { value: id }, name)));
  if (!WORKER_MODELS[agent]) {  // the models this worker lists (a local server's, OpenCode's), from Handoff
    getJSON(`/api/board/models?project=${encodeURIComponent(board.project)}&agent=${encodeURIComponent(agent)}`)
      .then((found) => {
        const models = Array.isArray(found.models) ? found.models.slice(0, 300) : [];
        // Free ones first: OpenCode's …-free models, and everything on this computer
        models.sort((x, y) => (/-free$/.test(y) ? 1 : 0) - (/-free$/.test(x) ? 1 : 0));
        list.replaceChildren(...models.map((m) => el("option", { value: m }, /-free$/.test(m) ? `${m} (free)` : m)));
        if (!names.one && found.default) names.one = found.default;
      })
      .catch(() => { /* any name can still be typed */ });
  }
  // What it can reach, and for one that runs with its own permissions, the person's yes before it can
  const about = el("small", { class: "worker-access" },
    `${ACCESS_WORDS[worker.access] || ""}${worker.local ? " Free: it runs on your computer." : ""}`);
  const enable = el("div", { class: "worker-enable" });
  const drawEnable = () => {
    if (!worker.needs_ok || worker.enabled) { enable.replaceChildren(); return; }
    const button = el("button", { type: "button", class: "btn", "data-key": "needs-enable" }, `Let ${worker.label || agent} change files`);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await postJSON("/api/board/action", { project: board.project, op: "agents.enable", args: { agent, on: true } });
        worker.enabled = true;
      } catch (e) {
        button.disabled = false;
        button.title = e.message || String(e);
      }
      drawEnable();
    });
    enable.replaceChildren(el("small", {}, `${worker.label || agent} waits for your yes before it changes files here.`), button);
  };
  drawEnable();
  // The crew Handoff picked, kept on asked.run so toggling Auto off and on (or a redraw) doesn't ask again
  const clip = (value, limit) => (typeof value === "string" ? value : "").slice(0, limit);
  const roleWord = (role, i) => {
    const text = (role || "").trim() || (i === 0 ? "Plans" : `Step ${i}`);
    return text.charAt(0).toLocaleUpperCase() + text.slice(1);
  };
  const crewNote = (crew) => {
    const job = (crew.job || "").trim();
    const size = (crew.size || "").trim();
    const which = size && job ? `A ${size} ${job} task. ` : (size || job ? `A ${size || job} task. ` : "");
    return `${which}Handoff picked these for this task. Change any model or effort; what you see is what runs.`;
  };
  const crewRowsFrom = (found) => {
    const roles = Array.isArray(found.roles) ? found.roles.filter((r) => r && typeof r === "object") : [];
    let made;
    if (roles.length) {
      made = roles.map((r) => ({
        role: clip(r.role, 80), model: clip(r.model, 80), effort: clip(r.effort, 10).trim().toLowerCase(),
        why: clip(r.why, 300),
      }));
    } else {
      const chain = Array.isArray(found.chain) ? found.chain.filter((m) => typeof m === "string") : [];
      const efforts = found.efforts && typeof found.efforts === "object" ? found.efforts : {};
      made = chain.map((model) => ({
        role: "", model: model.slice(0, 80), why: "",
        effort: typeof efforts[model] === "string" ? efforts[model].slice(0, 10).trim().toLowerCase() : "",
      }));
    }
    return made.slice(0, MAX_MODELS);  // approve seals at most four, and what is shown is what runs
  };
  const poke = () => {  // fallbacks follow the models; a crew that arrives has to say so, the toggle's change does
    if (crewRows && crewRows.isConnected) crewRows.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const setEfforts = async (high) => {
    const list = (state.crew && state.crew.rows) || [];
    list.forEach((row, i) => ask(row, i, "needs-crew-effort"));  // (a model typed a moment ago may still be looked up)
    await Promise.all([...asking.values()]);
    list.forEach((row, i) => {
      const choices = crewAllowed(row.model);  // lowest first; none at all stays at Default
      row.effort = choices.length ? choices[high ? choices.length - 1 : 0] : "";
      const old = crewRows && crewRows.querySelector(`[data-key="needs-crew-effort-${i}"]`);
      if (old) old.replaceWith(effortSelect(row, i, "needs-crew-effort"));  // (only the selects: the buttons keep focus)
    });
  };
  const drawCrew = () => {
    if (!usingCrew() || !crewRows) return;
    const crew = state.crew;
    if (!crew || !crew.settled) {
      crewStatus.textContent = "Picking a crew…";
      crewRows.replaceChildren();
      crewActions.replaceChildren();
      crewHint.textContent = "";
      return;
    }
    if (crew.error) {
      crewStatus.textContent = `Couldn't pick a crew: ${crew.error}`;
      crewRows.replaceChildren();
      crewActions.replaceChildren();
      crewHint.textContent = "";
      return;
    }
    const list = crew.rows || [];
    crewStatus.textContent = list.length ? "" : "Handoff didn't pick any models. Switch Auto off to pick them yourself.";
    crewRows.replaceChildren(...list.map((row, i) => {
      const role = roleWord(row.role, i);
      return el("div", { class: "crew-row" },
        el("span", { class: "crew-role" }, role),
        modelBox(row, i, "needs-crew-model", `Model for ${role.toLowerCase()}`),
        effortSelect(row, i, "needs-crew-effort"),
        el("small", { class: "crew-why" }, row.why || null));
    }));
    list.forEach((row, i) => ask(row, i, "needs-crew-effort"));
    const max = el("button", { type: "button", class: "btn", "data-key": "needs-crew-max" }, "All max");
    const low = el("button", { type: "button", class: "btn", "data-key": "needs-crew-low" }, "All low");
    max.addEventListener("click", () => setEfforts(true));
    low.addEventListener("click", () => setEfforts(false));
    crewActions.replaceChildren(...(list.length ? [max, low] : []));
    crewHint.textContent = list.length ? crewNote(crew) : "";
  };
  const ensureCrew = () => {
    const ref = (t && t.ref) || "";
    const crew = state.crew;
    if (crew && crew.ref === ref && crew.settled) { drawCrew(); return Promise.resolve(); }
    if (crew && crew.ref === ref && crew.pending) {  // a redraw while the ask is still out
      drawCrew();
      return crew.pending.then(() => {
        if (usingCrew() && crewRows && crewRows.isConnected) { drawCrew(); poke(); }
      });
    }
    const mine = (state.crewAsk = (state.crewAsk || 0) + 1);  // an older reply is ignored, as fallbackRow's asking
    state.crew = { ref, rows: [], how: "", job: "", size: "", error: "", settled: false, loading: true };
    drawCrew();
    const query = new URLSearchParams({ project: board.project, task: ref });
    const pending = getJSON(`/api/board/crew?${query}`)
      .then((found) => {
        if (mine !== state.crewAsk || !state.crew || state.crew.ref !== ref) return;
        const body = found && typeof found === "object" ? found : {};
        state.crew.rows = crewRowsFrom(body);
        state.crew.how = clip(body.how, 20);
        state.crew.job = clip(body.job, 20);
        state.crew.size = clip(body.size, 20);
        state.crew.error = "";
      })
      .catch((e) => {
        if (mine !== state.crewAsk || !state.crew || state.crew.ref !== ref) return;
        state.crew.rows = [];
        state.crew.error = (e && e.message) || "something went wrong";
      })
      .finally(() => {
        if (mine !== state.crewAsk || !state.crew || state.crew.ref !== ref) return;
        state.crew.loading = false;
        state.crew.settled = true;
        if (usingCrew() && crewRows && crewRows.isConnected) { drawCrew(); poke(); }
      });
    state.crew.pending = pending;
    return pending;
  };
  const applyAuto = () => {
    const on = usingCrew();
    modeLabel.hidden = on;
    mode.hidden = on;
    rows.hidden = on;
    hint.hidden = on;
    for (const node of [crewStatus, crewRows, crewActions, crewHint]) node.hidden = !on;
    if (!on) {
      crewRows.replaceChildren();
      crewActions.replaceChildren();
      crewStatus.textContent = "";
      crewHint.textContent = "";
      return;
    }
    ensureCrew();
  };
  let autoLabel = null;
  if (autoOn) {
    const autoBox = el("input", { type: "checkbox", id: "needs-auto", "data-key": "needs-auto" });
    autoBox.checked = !!state.auto;
    autoBox.addEventListener("change", () => { state.auto = autoBox.checked; applyAuto(); });
    autoLabel = el("label", { class: "auto-crew", for: "needs-auto" }, autoBox, "Auto: let Handoff pick the crew");
    crewStatus = el("small", { class: "crew-status", role: "status", hidden: true });
    crewRows = el("div", { class: "crew-rows", role: "group", "aria-label": "Crew Handoff picked", hidden: true });
    crewActions = el("div", { class: "crew-actions", hidden: true });
    crewHint = el("small", { class: "crew-hint", hidden: true });
  }
  const row = el("div", { class: "model-row" }, autoLabel, modeLabel, mode, rows, hint,
    crewStatus, crewRows, crewActions, crewHint, about, enable, list);
  if (autoOn) applyAuto();
  const chainArgs = (typed, takes, limit) => {
    // A blank row, or a model named twice, would only come back refused: leave them out. Auto also stops at
    // MAX_MODELS, which is all approve will seal.
    let picked = typed.filter((r, i) => r.model && typed.findIndex((o) => o.model === r.model) === i);
    if (limit) picked = picked.slice(0, limit);
    const models = picked.map((r) => r.model);
    // Only a level the model takes: one picked for the model typed before goes, and the new one thinks at its default
    const efforts = Object.fromEntries(picked.filter((r) => r.effort && takes(r.model).includes(r.effort))
      .map((r) => [r.model, r.effort]));
    return { models, ...(Object.keys(efforts).length ? { efforts } : {}) };
  };
  const args = () => {
    if (usingCrew()) {
      const list = (state.crew && state.crew.rows) || [];
      return chainArgs(list.map((r) => ({ model: tidyCrew(r.model), effort: r.effort })), crewAllowed, MAX_MODELS);
    }
    const typed = state.rows.map((r) => ({ model: tidy(r.model), effort: r.effort }));
    if (state.mode === "own") return typed[0].effort ? { efforts: { "": typed[0].effort } } : {};
    return chainArgs(typed, allowed);
  };
  // Before args(): the levels of every model as typed now (a model typed just before pressing the button is still
  // being looked up), and the crew, if Auto is waiting on it, and why it can't go yet, if it can't
  const ready = async () => {
    if (usingCrew()) await ensureCrew();
    const crew = usingCrew() && state.crew && state.crew.settled && !state.crew.error;
    const list = crew ? state.crew.rows : state.rows;
    const prefix = crew ? "needs-crew-effort" : "needs-effort";
    list.forEach((r, i) => ask(r, i, prefix));
    await Promise.all([...asking.values()]);
  };
  const problem = () => {
    if (worker.needs_ok && !worker.enabled) {
      return `Let ${worker.label || agent} change files first (the button above), or pick another agent.`;
    }
    if (usingCrew()) {
      if (!state.crew || !state.crew.settled) return "The crew is still being picked. Wait for it, then approve.";
      if (state.crew.error) return "Couldn't pick a crew. Switch Auto off to pick the models yourself.";
      if (!args().models.length) return "Name a model for the crew, or switch Auto off to pick the models yourself.";
      return "";
    }
    return state.mode !== "own" && !args().models.length ? `Name a model to run it on, or pick ${agent}'s own model.` : "";
  };
  return { row, args, ready, problem };
}

// How hard an answer or a review thinks: one level from the ones the worker takes, worded as the edit picker words
// them. What's picked lives in `asked.askEffort`, so a redraw keeps it. Returns the row and level() ("" for Default,
// which sends nothing).
function askEffortRow(asked, worker) {
  const select = el("select", { id: "needs-ask-effort", "data-key": "needs-ask-effort" },
    [["", "Default effort"], ...worker.efforts.map((l) => [l, `${EFFORT_WORDS[l] || l} effort`])]
      .map(([value, text]) => el("option", { value }, text)));
  if (![...select.options].some((o) => o.value === asked.askEffort)) asked.askEffort = "";
  select.value = asked.askEffort;
  select.addEventListener("change", () => { asked.askEffort = select.value; });
  const row = el("div", { class: "effort-row" }, el("label", { for: "needs-ask-effort" }, "How hard should it think?"),
    select, el("small", {}, "Higher effort thinks longer and uses more."));
  return { row, level: () => select.value };
}

// Who takes over when a model hits its usage limit: the next best models, from Handoff's benchmark rankings, that
// this computer has. Shown before the yes, with a switch, and one toggle per agent in the list (`state.off` holds the
// ones unticked; an entry "codex:gpt-6-astra" is agent "codex"); approve sends exactly the list shown (Handoff seals
// it), so a run goes on with the next model, and says so, instead of stopping with nothing done. Nothing is shown by
// a Handoff that can't say, and nothing is sent then. The toggles sit beside the line, not in it (its label is the
// sentence alone). Unticking the last agent switches the line off (the run stops at its limit, and says so);
// switching it on again brings every agent back.
function fallbackRow(asked, t, agent, kindNow, modelsNow) {
  const state = asked.fallback
    || (asked.fallback = { on: true, list: [], words: "", key: "", listKey: "", off: [] });
  state.off = state.off || [];
  const agentOf = (entry) => entry.split(":")[0];
  const kept = () => state.list.filter((entry) => !state.off.includes(agentOf(entry)));
  const box = el("input", { type: "checkbox", id: "needs-fallback", "data-key": "needs-fallback" });
  box.checked = state.on;
  box.addEventListener("change", () => {
    state.on = box.checked;
    if (state.on && !kept().length) state.off = [];  // (switched on with every agent unticked: they all come back)
  });
  const words = el("span", {});
  const row = el("label", { class: "fallback-row", for: "needs-fallback", hidden: true }, box, words);
  const agents = el("div", { class: "fallback-agents", role: "group", "aria-label": "Agents to go on with",
    hidden: true });
  let drawn = null;  // the agents the toggles were built for: rebuilt only when these change, so focus stays put
  let chips = [];  // [agent, checkbox] of each toggle
  const chipFor = (name, i) => {
    const input = el("input", { type: "checkbox", id: `needs-fallback-agent-${i}`,
      "data-key": `needs-fallback-agent:${name}`, "aria-label": `Go on with ${name}` });
    input.addEventListener("change", () => {
      state.off = state.off.filter((n) => n !== name);
      if (!input.checked) state.off.push(name);
      if (!kept().length) state.on = false;  // (the last agent unticked: the switch goes off, and the chips with it)
    });
    return [name, input];
  };
  // A new pick starts the choice over: every agent ticked again, and the switch back on if unticking the last agent
  // was what switched it off (the chips are disabled while it's off). Run it before state.list is replaced.
  const startOver = () => {
    if (state.list.length && !kept().length) state.on = true;
    state.off = [];
  };
  // (the line stays as it is while the list for a new pick comes, so the form doesn't jump under the pointer)
  const draw = () => {
    const left = kept();
    const gone = !state.list.length || kindNow() === "image";
    row.hidden = gone;
    agents.hidden = gone;
    const unticked = left.length < state.list.length;  // (something unticked: the line is what's left, not the list)
    const shown = unticked ? left.join(", then ") : state.words || state.list.join(", then ");
    words.textContent = state.list.length && !left.length
      ? "If a model hits its usage limit, the run stops there"  // (every agent unticked: nothing to go on with)
      : `If a model hits its usage limit, go on with ${shown}`;
    box.checked = state.on && left.length > 0;  // (unticking the last agent already switched it off)
    const names = [...new Set(state.list.map(agentOf))];
    if (names.join("\n") !== drawn) {
      drawn = names.join("\n");
      chips = names.map(chipFor);
      agents.replaceChildren(...chips.map(([name, input]) => el("label", { class: "fallback-agent" }, input, name)));
    }
    for (const [name, input] of chips) {
      input.checked = !state.off.includes(name);
      input.disabled = !state.on;
    }
  };
  let asking = 0;
  let pending = Promise.resolve();  // the list for what's picked now, on its way
  const refresh = () => {
    const kind = kindNow();
    const models = modelsNow() || [];
    const key = `${agent}|${kind}|${models.join(",")}`;
    if (key === state.key) { draw(); return pending; }
    state.key = key;
    if (kind === "image" || !agent) {
      if (key !== state.listKey) startOver();  // (a new pick: the choice starts over)
      state.list = [];
      state.listKey = key;
      draw();
      return (pending = Promise.resolve());
    }
    const mine = ++asking;
    const query = new URLSearchParams({ project: board.project, agent, kind, models: models.join(",") });
    pending = getJSON(`/api/board/fallbacks?${query}`)
      .then((found) => {
        if (mine !== asking) return;  // (something newer was picked meanwhile)
        if (key !== state.listKey) startOver();  // (a new pick starts over; a redraw, the same key, keeps it)
        state.list = Array.isArray(found.fallbacks) ? found.fallbacks.filter((f) => typeof f === "string").slice(0, 5) : [];
        state.words = typeof found.words === "string" ? found.words : "";
      })
      .catch(() => {  // (a run without fallbacks stops at a limit, as before)
        if (mine !== asking) return;
        if (key !== state.listKey) startOver();
        state.list = [];
      })
      .finally(() => { if (mine === asking) { state.listKey = key; draw(); } });
    return pending;
  };
  state.key = "";
  refresh();
  // Only the list shown for what's picked now, less the agents unticked: approve seals it
  const args = () => {
    const left = kept();
    return state.on && left.length && state.listKey === state.key && kindNow() !== "image" ? { fallbacks: left } : {};
  };
  return { row, agents, refresh, args };
}

// What the person typed or picked lives in `asked.draft`, so a redraw (or a refusal) keeps it
function needsForm(asked, t) {
  const a = asked.action;
  const form = el("form", { class: "needs" });
  let read;
  let check = async () => "";  // why what was picked can't go yet, if it can't ("" when it can)
  if (a.needs === "text") {
    const box = el("textarea", { rows: "3", maxlength: "4000", "data-first": true, id: "needs-text", "data-key": "needs-text",
      required: TEXT_NEEDED[a.op] || false, ...PRIVATE_TYPING });
    box.value = asked.draft || "";
    box.addEventListener("input", () => { asked.draft = box.value; });
    form.append(el("label", { for: "needs-text" }, TEXT_LABEL[a.op] || "Note"), box);
    read = () => ({ ...(a.args || {}), [TEXT_ARG[a.op] || "text"]: box.value.trim() });
  } else if (a.needs === "agent") {
    const select = agentSelect(asked.draft !== undefined ? asked.draft : t.assignee || "", "needs-agent");
    select.setAttribute("data-first", "");
    select.setAttribute("data-key", "needs-agent");
    select.addEventListener("change", () => { asked.draft = select.value; });
    form.append(el("label", { for: "needs-agent" }, "Assign to"), select);
    read = () => ({ ...(a.args || {}), to: select.value });
  } else {
    const kinds = a.kinds && a.kinds.length ? a.kinds : [(a.args && a.args.kind) || "edit"];
    const chosen = asked.draft || (a.args && a.args.kind) || kinds[0];
    const options = kinds.map((kind, i) => {
      const radio = el("input", { type: "radio", name: "needs-kind", value: kind, checked: kind === chosen,
        "data-first": i === 0, "data-key": `needs-kind:${kind}` });
      radio.addEventListener("change", () => { asked.draft = kind; });
      return el("label", { class: "kind" }, radio,
        el("span", {}, el("b", {}, (t.targets && t.targets[kind] && kind === "review" ? "Review it" : KIND_WORDS[kind]) || kind),
          el("small", {}, `${(a.args && a.args.agent) || t.assignee} ${kindHint(kind, t.targets && t.targets[kind])}`)));
    });
    form.append(el("fieldset", {}, el("legend", {}, "What should it do?"), options));
    const kindNow = () => (form.querySelector("input[name=needs-kind]:checked") || {}).value || chosen;
    // Which model the edit runs on, or a chain of them, and how hard each thinks (a Handoff that says what its
    // workers take: the task's `workers`)
    const agent = (a.args && a.args.agent) || t.assignee;
    const worker = t.workers && t.workers[agent];
    const picker = worker && kinds.includes("edit") ? runPicker(asked, agent, worker, t) : null;
    if (picker) {
      const show = () => { picker.row.hidden = kindNow() !== "edit"; };
      form.addEventListener("change", show);
      show();
      form.append(picker.row);
    }
    // An answer or a review thinks at one level too (no models to pick: the worker's own, and its levels)
    const asks = (kind) => kind === "answer" || kind === "review";
    const askEffort = worker && worker.efforts && worker.efforts.length && kinds.some(asks) ? askEffortRow(asked, worker) : null;
    if (askEffort) {
      const showEffort = () => { askEffort.row.hidden = !asks(kindNow()); };
      form.addEventListener("change", showEffort);
      showEffort();
      form.append(askEffort.row);
    }
    const backup = fallbackRow(asked, t, agent, kindNow, () => (picker && kindNow() === "edit" ? picker.args().models : null));
    form.addEventListener("change", backup.refresh);
    form.append(backup.row, backup.agents);
    read = () => {
      const kind = kindNow();
      const level = askEffort && asks(kind) ? askEffort.level() : "";
      return { ...(a.args || {}), kind, ...(picker && kind === "edit" ? picker.args() : {}),
        ...(level ? { efforts: { "": level } } : {}), ...backup.args() };
    };
    check = async () => {
      if (!picker || kindNow() !== "edit") { await backup.refresh(); return ""; }
      await picker.ready();
      const problem = picker.problem();
      await backup.refresh();  // (the list for the models as typed now, which approve seals)
      return problem;
    };
  }
  const cancel = el("button", { type: "button", class: "btn", "data-key": "needs-cancel" }, "Cancel");
  cancel.addEventListener("click", closeNeeds);
  form.append(el("div", { class: "needs-buttons" }, cancel,
    el("button", { type: "submit", class: "btn primary", disabled: busy, "data-key": "needs-submit" }, a.label)));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    const problem = await check();
    if (problem) {
      actionFailure = problem;
      renderPanel();
      return;
    }
    if (!busy) act(a, t, read());  // the form says what will happen: pressing its button is the yes
  });
  form.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); closeNeeds(); } });
  return form;
}

function confirmWords(a, t, args) {
  const agent = args.agent || (t.run && t.run.agent) || t.assignee;
  const kind = args.kind || (t.run && t.run.kind) || "edit";
  if (a.op === "approve" || a.op === "run.start") {
    const of = (t.run && t.run.of) || (t.targets && t.targets[kind]);
    return { title: `Run ${t.ref} now?`, text: `${agent} ${kindHint(kind, of)} Its result comes back to this board.`,
      yes: "Run it now" };
  }
  if (a.op === "delete") {
    return { title: `Delete ${t.ref}?`, text: "Its history and results go too. This can't be undone.", yes: "Delete" };
  }
  if (a.op === "merge") {
    return { title: `Merge ${t.ref}?`, text: "Its branch merges into the checked-out branch here. Nothing is pushed.", yes: a.label };
  }
  if (isDanger(a)) return { title: `Cancel ${t.ref}?`, text: "It moves to Done as cancelled. You can reopen it later.", yes: "Cancel the task" };
  return { title: `${a.label}?`, text: t.title, yes: a.label };
}

async function confirmThen(a, t, args) {
  if (a.confirm) {
    const words = confirmWords(a, t, args);
    if (!(await confirmDialog(words.title, words.text, words.yes, isDanger(a)))) return;
  }
  await act(a, t, args);
}

async function act(a, t, args) {
  if (busy) return;  // one at a time: a double click mustn't send it twice
  busy = true;
  actionFailure = "";
  actionNote = "";
  renderPanel();
  const where = board.project;
  // An approval seals the task as the panel shows it: if an agent changed it since, Handoff refuses
  // (a Handoff that doesn't send content doesn't take this either)
  const seen = a.op === "approve" && detail && detail.task && detail.task.ref === t.ref && detail.task.content
    ? { shown: detail.task.content } : {};
  try {
    const reply = await postJSON("/api/board/action", { project: where, op: a.op, args: { ...args, ...seen, task: t.ref } });
    // As a rule nothing's left; when something is (the agent's worktree and branch), Handoff says what, and how
    // to remove it
    if (a.op === "delete") deleteLeft = Array.isArray(reply && reply.left) ? reply.left.join(" ") : "";
    // A merge only ever lands on this machine: the branch goes into the checked-out one, nothing is pushed
    if (a.op === "merge") actionNote = "Merged into the checked-out branch. Nothing was pushed.";
    // "Run it now" approves the run, then starts it (as `handoff run T-N` would). Once approved, the
    // form is done with even if the start fails: the panel then offers Run it now on its own.
    if (a.op === "approve") {
      needs = null;
      await postJSON("/api/board/action", { project: where, op: "run.start", args: { task: t.ref } });
    }
    needs = null;
  } catch (e) {
    actionFailure = e.message;
    if (e.code === "changed") needs = null;  // show what it is now, to approve again
  } finally {
    busy = false;
  }
  if (a.op === "delete" && !actionFailure) {
    closePanel();
    await refresh();
    return;
  }
  await refresh();
  if (openRef !== t.ref) return;
  if (needs) renderPanel();  // refused: keep what they typed, and say why
  else await loadTask(t.ref, true);
}

// ── Dialogs ───────────────────────────────────────────────────────────────

function showDialog(...children) {
  const dialog = $("#board-dialog");
  dialog.replaceChildren(...children);
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue), { once: true }));
}

function dialogButtons(yes, danger) {
  const cancel = el("button", { type: "button", class: "btn" }, "Cancel");
  cancel.addEventListener("click", () => $("#board-dialog").close("cancel"));
  return el("div", { class: "dialog-buttons" }, cancel,
    el("button", { type: "submit", class: `btn ${danger ? "danger-fill" : "primary"}`, value: "yes" }, yes));
}

async function confirmDialog(title, text, yes, danger) {
  const answer = showDialog(el("form", { method: "dialog" }, el("h2", {}, title), el("p", {}, text), dialogButtons(yes, danger)));
  $("#board-dialog .dialog-buttons .btn:last-child").focus();
  return (await answer) === "yes";
}

function getAgents() {
  const where = board.project;
  if (!agents || agents.project !== where) {
    agents = {
      project: where,
      promise: getJSON(`/api/board/agents?project=${encodeURIComponent(where)}`)
        .then((data) => (data.agents || []).filter((a) => a && typeof a.name === "string"))
        .then((list) => (list.length ? list : FALLBACK_AGENTS.map((name) => ({ name }))))
        .catch(() => FALLBACK_AGENTS.map((name) => ({ name }))),
    };
  }
  return agents.promise;
}

function agentSelect(selected, id) {
  const select = el("select", { id });
  const fill = (list) => {
    if (select.options.length) selected = select.value;  // what the person picked while the list was coming
    const names = list.map((a) => a.name).filter((n) => n !== "human");
    if (selected && selected !== "human" && !names.includes(selected)) names.unshift(selected);
    select.replaceChildren(el("option", { value: "" }, "Nobody yet"), el("option", { value: "human" }, "You"),
      ...names.map((n) => {
        const found = list.find((a) => a.name === n);
        const label = found && found.label && found.label !== n ? `${found.label} (${n})` : n;
        return el("option", { value: n }, label);
      }));
    select.value = selected;
  };
  fill([]);
  select.append(el("option", { disabled: true }, "Finding your agents…"));
  getAgents().then(fill);
  return select;
}

async function newTask() {
  if (!board || !board.exists) {
    if (!project) {
      askForProject("new");
    } else if (board) {
      hint = "start";
      render();
      const begin = $("#board-main .board-start");
      if (begin) begin.focus();
    } else {
      $("#board-project-input").focus();  // still being read, or it couldn't be: the line below the box says which
    }
    return;
  }
  const title = el("input", { type: "text", id: "new-title", maxlength: "200", required: true, ...PRIVATE_TYPING });
  const body = el("textarea", { id: "new-body", rows: "4", maxlength: "20000", ...PRIVATE_TYPING });
  const checks = el("textarea", { id: "new-checks", rows: "3", placeholder: "One check per line", ...PRIVATE_TYPING });
  const assignee = agentSelect("", "new-assignee");
  const field = (id, label, input, hint) => el("div", { class: "field" }, el("label", { for: id }, label), input,
    hint ? el("small", {}, hint) : null);
  const problem = el("div", { class: "dialog-problem", role: "alert" });
  const form = el("form", { method: "dialog" },
    el("h2", {}, "New task"),
    field("new-title", "Title", title),
    field("new-body", "Details", body, "What to do, and anything it needs to know."),
    field("new-checks", "Done when", checks),
    field("new-assignee", "Assign to", assignee),
    problem,
    dialogButtons("Add task", false));
  const where = board.project;
  let added = null;
  let pending = null;
  let closed = false;
  // The dialog stays open, with what was typed, until the task is on the board
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const buttons = [...form.querySelectorAll("button")];
    if (buttons.some((b) => b.disabled) || pending) return;
    const args = {
      title: title.value.trim(), body: body.value.trim(),
      acceptance: checks.value.split("\n").map((line) => line.trim()).filter(Boolean),
    };
    if (assignee.value) args.assignee = assignee.value;
    buttons.forEach((b) => { b.disabled = true; });
    problem.replaceChildren();
    pending = postJSON("/api/board/action", { project: where, op: "add", args });
    try {
      added = await pending;
      if (!closed) $("#board-dialog").close("yes");  // only this dialog, never one opened since
    } catch (err) {
      pending = null;
      if (closed) return;
      problem.replaceChildren(el("div", { class: "board-notice error" }, icon("alert"),
        el("span", {}, `Couldn't add the task: ${err.message}`)));
      buttons.forEach((b) => { b.disabled = false; });
    }
  });
  const answer = showDialog(form).then((value) => { closed = true; return value; });
  title.focus();
  await answer;
  if (pending) await pending.catch(() => {});
  if (!added || !board || board.project !== where) return;
  await refresh();
  if (added.task && added.task.ref) openPanel(added.task.ref);
}
