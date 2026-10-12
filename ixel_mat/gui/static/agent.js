// Agent (beta): a run of Ixel's agent in a folder you pick, on a model you already set up. The model only
// asks for one action at a time; Ixel reads files itself and anything that changes your computer (a write,
// an edit, a command) waits here for your Approve. Everything a run holds (thoughts, results, diffs, the
// answer) is untrusted: it only ever becomes text nodes, never HTML. The API is in AGENT_BETA.md.

import { $, el, icon, api, getJSON, postJSON, plural, copyButton, PRIVATE_TYPING } from "./common.js";
import { renderMarkdown } from "./markdown.js";

const POLL_EVERY = 1000;  // a live run is read this often while the page is on screen
const ARGS_SHOWN = 80;  // characters of an action's args in a step's one-line summary

const STATUS_WORDS = {
  running: "Running", waiting: "Waiting for you", done: "Done", stopped: "Stopped", failed: "Failed",
};
const LIVE = new Set(["running", "waiting"]);
const WRITES = new Set(["write_file", "edit_file"]);
const FILES = [
  ["soul", "SOUL.md", "The agent's persona: how it works and talks."],
  ["user", "USER.md", "Who you are, so it doesn't have to ask."],
  ["memory", "MEMORY.md", "What it has learned. It adds to this with remember; you can change anything."],
];

let models = null;     // GET /api/agent/models
let runs = null;       // GET /api/agent/runs
let files = null;      // GET /api/agent/files
let run = null;        // the run on screen: GET /api/agent/run
let runId = "";        // the run on screen; the page keeps nothing in browser storage, so a reload finds it in the run list
let prefilled = false; // the form has been filled from the newest earlier run once
let shownSig = "";     // the run as last drawn: a poll that changed nothing redraws nothing
let polling = null;
let reading = false;   // a poll is still waiting for its reply
const openSteps = new Set();  // steps whose result is expanded
const boxes = {};      // the Memory panel's text boxes, by name
const drafts = {};     // what you've typed in them and not saved yet, kept while you switch tabs

// The form's pieces, built once in start()
let modelPick = null;
let workspaceBox = null;
let goalBox = null;
let startButton = null;

export function start() {
  buildForm();
  $("#agent-stop").addEventListener("click", (e) => stopRun(e.currentTarget));
  $("#agent-memory-button").addEventListener("click", () => showMemory($("#agent-memory").hidden));
}

// Memory and skills take the place of the run while they're open, so each file gets the room to edit it
function showMemory(on) {
  $("#agent-memory").hidden = !on;
  $("#agent-work").hidden = on;
  const button = $("#agent-memory-button");
  button.setAttribute("aria-expanded", on ? "true" : "false");
  button.classList.toggle("on", on);
  if (on && files) renderMemory();
}

// A fresh form: the run on screen goes back to the list
function newRun() {
  stopPolling();
  run = null;
  runId = "";
  shownSig = "";
  openSteps.clear();
  showMemory(false);
  renderRun();
  renderRuns();
  updateButtons();
  goalBox.focus();
}

export function shown() {
  if (!models) loadModels();
  if (!files) loadFiles();
  loadRuns();
  if (!runId) renderRun();
  if (runId && (!run || LIVE.has(run.status))) refresh().then(() => { if (isLive()) poll(); });
}

export function hidden() {
  stopPolling();
}

// ── Small pieces ──────────────────────────────────────────────────────────

const isLive = () => Boolean(run && LIVE.has(run.status));

// The one request common.js has no helper for: saving a memory file
async function putJSON(path, body) {
  const res = await api(path, { method: "PUT", body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

// Every request that fails says so here, on the page
function setStatus(text) {
  const box = $("#agent-status");
  box.hidden = !text;
  box.replaceChildren(text ? el("div", { class: "board-notice error", role: "alert" }, icon("alert"), el("span", {}, text)) : "");
}

function clip(text, most) {
  const flat = String(text).replace(/\s+/g, " ").trim();
  return flat.length > most ? `${flat.slice(0, most - 1)}…` : flat;
}

// One line for what an action was asked to do: the path, the pattern, the command, or the start of whatever else
function argsSummary(args) {
  if (!args || typeof args !== "object") return "";
  if (Array.isArray(args.argv)) return clip(commandLine(args.argv), ARGS_SHOWN);
  const parts = [];
  if (args.path !== undefined && args.path !== null) parts.push(String(args.path));
  if (args.pattern !== undefined && args.pattern !== null) parts.push(`“${args.pattern}”`);
  if (args.name !== undefined && args.name !== null) parts.push(String(args.name));
  if (parts.length) return clip(parts.join(" · "), ARGS_SHOWN);
  const first = Object.values(args).find((v) => v !== undefined && v !== null && v !== "");
  if (first === undefined) return "";
  return clip(typeof first === "string" ? first : JSON.stringify(first), ARGS_SHOWN);
}

// The exact command, each argument quoted the way a shell would need it (it never goes through one)
function commandLine(argv) {
  return argv.map((a) => {
    const s = String(a);
    return /^[\w@%+=:,./-]+$/.test(s) ? s : `'${s.replace(/'/g, "'\\''")}'`;
  }).join(" ");
}

// When a run started: the server's seconds (or milliseconds) since 1970
function when(started) {
  if (!started) return "";
  const at = new Date(started < 1e12 ? started * 1000 : started);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function money(cost) {
  const c = Number(cost) || 0;
  if (c === 0) return "$0";
  return c < 0.01 ? `$${c.toFixed(4)}` : `$${c.toFixed(2)}`;
}

// ── The form ──────────────────────────────────────────────────────────────

function buildForm() {
  modelPick = el("select", { id: "agent-model" }, el("option", { value: "" }, "Loading the models…"));
  workspaceBox = el("input", { type: "text", id: "agent-workspace", class: "mono", ...PRIVATE_TYPING,
    placeholder: "A folder in your home folder, like ~/code/shop" });
  goalBox = el("textarea", { id: "agent-goal", rows: "3", ...PRIVATE_TYPING,
    placeholder: "What should it do? It reads on its own and asks before it changes anything." });
  goalBox.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); startRun(); }
  });
  startButton = el("button", { type: "submit", class: "btn primary" }, icon("play"), el("span", {}, "Start"));
  const form = $("#agent-form");
  form.replaceChildren(
    el("div", { class: "agent-form-row" },
      el("div", { class: "field" }, el("label", { for: "agent-model" }, "Model"), modelPick),
      el("div", { class: "field agent-workspace" }, el("label", { for: "agent-workspace" }, "Workspace folder"), workspaceBox)),
    el("div", { class: "field" }, el("label", { for: "agent-goal" }, "Goal"), goalBox),
    el("div", { class: "agent-form-buttons" },
      el("small", { class: "fine" }, "Reads run at once. Writes, edits and commands wait for your approval."),
      el("span", { class: "spacer" }), startButton));
  form.addEventListener("submit", (e) => { e.preventDefault(); startRun(); });
}

async function loadModels() {
  try {
    const data = await getJSON("/api/agent/models");
    models = data.models || [];
  } catch (e) {
    setStatus(`Couldn't list the models: ${e.message}`);
    modelPick.replaceChildren(el("option", { value: "" }, "No models"));
    return;
  }
  const ready = models.filter((m) => m.ready);
  const pick = ready[0];
  modelPick.replaceChildren(...models.map((m) => el("option", { value: m.name, disabled: !m.ready },
    m.ready ? (m.label || m.name) : `${m.label || m.name} (not set up)`)));
  if (!models.length) modelPick.replaceChildren(el("option", { value: "" }, "No models: set one up in Settings"));
  else if (!ready.length) modelPick.prepend(el("option", { value: "" }, "No model is ready: set one up in Settings"));
  modelPick.value = pick ? pick.name : "";
  updateButtons();
  prefill();
}

// Once the models and the run list are both in, fill the form from the newest earlier run and
// reopen a run that is still going. Only the first time, so it never overwrites what you type.
function prefill() {
  if (prefilled || !models || !runs) return;
  prefilled = true;
  if (!runs.length) return;
  const newest = runs.reduce((a, b) => (startedMs(b) > startedMs(a) ? b : a));
  if (!workspaceBox.value.trim() && newest.workspace) workspaceBox.value = newest.workspace;
  const same = models.find((m) => m.ready && m.name === newest.model);
  if (same) modelPick.value = same.name;
  updateButtons();
  const live = runs.find((r) => LIVE.has(r.status));
  if (live && !runId) show(live.id);
}

// When a run started, in milliseconds (the server sends seconds or milliseconds)
function startedMs(r) {
  const s = Number(r.started) || 0;
  return s && s < 1e12 ? s * 1000 : s;
}

async function startRun() {
  if (isLive()) return;
  const model = modelPick.value;
  const workspace = workspaceBox.value.trim();
  const goal = goalBox.value.trim();
  if (!model) { setStatus("Pick a model first."); modelPick.focus(); return; }
  if (!workspace) { setStatus("Type the folder it should work in."); workspaceBox.focus(); return; }
  if (!goal) { setStatus("Type what it should do."); goalBox.focus(); return; }
  startButton.disabled = true;
  try {
    const reply = await postJSON("/api/agent/start", { model, workspace, goal });
    setStatus("");
    show(reply.id);
  } catch (e) {
    setStatus(`Couldn't start the run: ${e.message}`);
  } finally {
    updateButtons();
  }
}

function updateButtons() {
  const live = isLive();
  startButton.disabled = live || !modelPick.value;
  $("#agent-stop").hidden = !live;
}

// ── The run ───────────────────────────────────────────────────────────────

// Puts a run on screen (a new one, or one picked from the list) and follows it while it's live
async function show(id) {
  stopPolling();
  showMemory(false);
  runId = id;
  run = null;
  shownSig = "";
  openSteps.clear();
  await refresh();
  if (isLive()) poll();
  renderRuns();
}

async function refresh() {
  if (!runId) return;
  const before = run ? run.status : "";
  try {
    run = await getJSON(`/api/agent/run?id=${encodeURIComponent(runId)}`);
  } catch (e) {
    if (e.status === 404) {
      run = null;
      runId = "";
    } else {
      setStatus(`Couldn't read the run: ${e.message}`);
    }
  }
  renderRun();
  if (run && before && before !== run.status) loadRuns();  // the list shows each run's status
}

function poll() {
  stopPolling();
  polling = setInterval(async () => {
    if ($("#view-agent").hidden || !runId) { stopPolling(); return; }
    if (reading) return;
    reading = true;
    try { await refresh(); } finally { reading = false; }
    if (!isLive()) stopPolling();
  }, POLL_EVERY);
}

function stopPolling() {
  if (polling) clearInterval(polling);
  polling = null;
}

async function stopRun(button) {
  if (!run) return;
  button.disabled = true;
  try {
    run = await postJSON("/api/agent/stop", { id: run.id });
  } catch (e) {
    setStatus(`Couldn't stop the run: ${e.message}`);
  } finally {
    button.disabled = false;
  }
  renderRun();
  loadRuns();
}

async function decide(approve, always, card) {
  for (const b of card.querySelectorAll("button")) b.disabled = true;
  try {
    run = await postJSON("/api/agent/decide", { id: run.id, approve, always });
  } catch (e) {
    setStatus(`Couldn't send your answer: ${e.message}`);
    for (const b of card.querySelectorAll("button")) b.disabled = false;
    return;
  }
  renderRun();
  if (isLive()) poll();
}

function renderRun() {
  updateButtons();
  $("#agent-sub").textContent = run ? [STATUS_WORDS[run.status] || run.status, run.model].filter(Boolean).join(" · ") : "";
  const sig = JSON.stringify(run);
  if (sig === shownSig) return;
  shownSig = sig;
  const box = $("#agent-run");
  if (!run) { box.replaceChildren(emptyState()); return; }

  const parts = [el("header", { class: "agent-run-head" },
    el("h2", {}, run.goal),
    el("p", { class: "agent-run-meta" },
      el("span", { class: `agent-state s-${run.status}` }, STATUS_WORDS[run.status] || run.status),
      el("span", { class: "mono" }, run.workspace),
      el("span", {}, when(run.started))))];
  const steps = run.steps || [];
  if (steps.length) parts.push(el("ol", { class: "agent-steps", "aria-label": "Steps" }, steps.map(stepItem)));
  else if (isLive()) parts.push(el("p", { class: "agent-thinking" }, el("span", { class: "spin" }), "Thinking about the first step…"));
  if (run.status === "waiting" && run.pending) parts.push(approvalCard(run.pending));
  else if (run.status === "running" && steps.length) {
    parts.push(el("p", { class: "agent-thinking" }, el("span", { class: "spin" }), "Working on the next step…"));
  }
  if (run.status === "done" && run.answer) {
    parts.push(el("section", { class: "agent-answer", "aria-label": "Answer" },
      el("header", {}, el("h3", {}, "Answer"), el("span", { class: "spacer" }), copyButton(() => run.answer, "Copy the answer")),
      el("div", { class: "md" }, renderMarkdown(run.answer))));
  }
  if (run.status === "failed") {
    parts.push(el("div", { class: "board-notice error", role: "alert" }, icon("alert"),
      el("span", { class: "notice-lines" }, run.error || "The run failed.")));
  }
  if (run.status === "stopped") {
    parts.push(el("div", { class: "board-notice", role: "status" }, icon("stop"),
      el("span", { class: "notice-lines" }, run.error || "Stopped.")));
  }
  const usage = run.usage || {};
  parts.push(el("p", { class: "agent-usage" },
    `${plural(Number(usage.calls) || 0, "model call")} · ${money(usage.cost)}`));
  box.replaceChildren(...parts);
}

// What a run does, before there is one on screen
function emptyState() {
  const point = (name, title, text) => el("li", {}, icon(name), el("div", {}, el("strong", {}, title), el("span", {}, text)));
  return el("div", { class: "agent-empty" },
    el("h2", {}, "How the agent works"),
    el("ul", {},
      point("file", "It reads on its own", "It lists, reads and searches files in the folder you pick, and nothing outside it."),
      point("lock", "It asks before it changes anything", "Every edit shows you a diff, and every command shows the exact line, before you approve it."),
      point("book", "It remembers", "SOUL, USER and MEMORY files and its skills live under Memory and skills, on this computer.")),
    el("p", { class: "fine" }, "Pick an earlier run on the right to see what it did."));
}

function stepItem(step) {
  const failed = step.ok === false;
  const finish = step.action === "finish";  // its answer has a card of its own below the steps
  const hasResult = !finish && step.result !== undefined && step.result !== null && step.result !== "";
  const result = hasResult
    ? el("details", { class: "agent-result", open: openSteps.has(step.n),
      ontoggle: (e) => { if (e.currentTarget.open) openSteps.add(step.n); else openSteps.delete(step.n); } },
      el("summary", {}, failed ? "Failed: show what happened" : "Result"),
      el("pre", { class: failed ? "failed" : "ok" }, String(step.result)))
    : null;
  return el("li", { class: `agent-step ${failed ? "failed" : "ok"}` },
    el("div", { class: "agent-step-head" },
      el("span", { class: "agent-step-n" }, String(step.n)),
      el("span", { class: "agent-step-action mono" }, step.action),
      el("span", { class: "agent-step-args mono" }, finish ? "" : argsSummary(step.args)),
      failed ? el("span", { class: "agent-step-flag", title: "This action failed" }, icon("x")) : null),
    step.thought ? el("p", { class: "agent-step-thought" }, step.thought) : null,
    result);
}

// What the run waits for: the change or the exact command, and your answer
function approvalCard(pending) {
  const command = pending.action === "run_command";
  const args = pending.args || {};
  const card = el("section", { class: "agent-approval", "aria-label": "Waiting for your approval" });
  const body = [];
  if (command) {
    const argv = Array.isArray(pending.argv) && pending.argv.length ? pending.argv : (args.argv || []);
    body.push(el("p", { class: "agent-approval-what" }, "It wants to run this command in the workspace:"),
      el("pre", { class: "agent-command" }, commandLine(argv)),
      pending.sandboxed
        ? el("p", { class: "agent-sandbox ok" }, icon("lock"),
          el("span", {}, "Runs in a sandbox: no network, and it can write only in the workspace and the temp folder."))
        : el("p", { class: "agent-sandbox no" }, icon("alert"),
          el("span", {}, "Not sandboxed: it runs with your account's access (without the keys saved in Ixel).")));
  } else if (WRITES.has(pending.action)) {
    body.push(el("p", { class: "agent-approval-what" },
      pending.action === "write_file" ? "It wants to write " : "It wants to edit ",
      el("code", {}, String(args.path || "a file")), ":"),
      diffBlock(pending.diff || ""));
  } else {
    body.push(el("p", { class: "agent-approval-what" }, `It wants to ${pending.action}: `,
      el("code", {}, argsSummary(args))));
  }
  const buttons = [
    el("button", { type: "button", class: "btn primary", onclick: () => decide(true, false, card) }, icon("check"), el("span", {}, "Approve")),
    el("button", { type: "button", class: "btn danger", onclick: () => decide(false, false, card) }, icon("x"), el("span", {}, "Deny")),
  ];
  if (command) {
    buttons.push(el("button", { type: "button", class: "btn", onclick: () => decide(true, true, card),
      title: "Run it, and from now on run commands that start this way in this workspace without asking" },
    el("span", {}, "Always allow")));
  }
  card.append(
    el("header", {}, icon("alert"), el("h3", {}, `Step ${pending.n} waits for you`)),
    ...body,
    el("div", { class: "agent-approval-buttons" }, ...buttons));
  return card;
}

// A unified diff, each line its own element so a change shows in colour
function diffBlock(diff) {
  if (!diff) return el("p", { class: "fine" }, "No changes to show.");
  const lines = diff.replace(/\r\n?/g, "\n").replace(/\n$/, "").split("\n");
  return el("pre", { class: "agent-diff" }, lines.map((line) => {
    let kind = "same";
    if (line.startsWith("+++") || line.startsWith("---")) kind = "file";
    else if (line.startsWith("@@")) kind = "hunk";
    else if (line.startsWith("+")) kind = "add";
    else if (line.startsWith("-")) kind = "del";
    return el("span", { class: `diff-line ${kind}` }, line || " ");
  }));
}

// ── Earlier runs ──────────────────────────────────────────────────────────

async function loadRuns() {
  try {
    const data = await getJSON("/api/agent/runs");
    runs = data.runs || [];
  } catch (e) {
    setStatus(`Couldn't list the earlier runs: ${e.message}`);
  }
  renderRuns();
  prefill();
}

function renderRuns() {
  const box = $("#agent-runs");
  const head = el("div", { class: "agent-side-head" }, el("h2", { class: "agent-side-title" }, "Runs"),
    el("button", { type: "button", class: "btn small", onclick: newRun }, icon("plus"), el("span", {}, "New run")));
  if (!runs) { box.replaceChildren(head, el("div", { class: "set-loading" }, el("span", { class: "spin" }), "Looking…")); return; }
  if (!runs.length) { box.replaceChildren(head, el("p", { class: "fine" }, "None yet. Each run you start is kept here.")); return; }
  box.replaceChildren(head, el("ul", { class: "agent-run-list" }, runs.map((r) => el("li", {},
    el("button", { type: "button", class: "agent-run-pick", "aria-current": r.id === runId ? "true" : null,
      onclick: () => show(r.id) },
    el("span", { class: "agent-run-goal" }, clip(r.goal || "(no goal)", 120)),
    el("span", { class: "agent-run-when" },
      el("span", { class: `agent-state s-${r.status}` }, STATUS_WORDS[r.status] || r.status),
      when(r.started) ? ` · ${when(r.started)}` : ""))))));
}

// ── Memory ────────────────────────────────────────────────────────────────

async function loadFiles() {
  try {
    files = await getJSON("/api/agent/files");
  } catch (e) {
    setStatus(`Couldn't read the agent's memory: ${e.message}`);
    $("#agent-memory").replaceChildren(el("h2", { class: "agent-side-title" }, "Memory"),
      el("p", { class: "fine" }, "Not loaded."));
    return;
  }
  renderMemory();
}

let memoryTab = "soul";  // the file open in Memory and skills

function renderMemory() {
  const tabs = [...FILES.map(([name, file]) => [name, file]), ["skills", "Skills"]];
  const bar = el("div", { class: "tabs", role: "tablist", "aria-label": "Memory and skills" }, tabs.map(([key, title]) =>
    el("button", { type: "button", role: "tab", class: "tab", id: `agent-tab-${key}`, "aria-controls": "agent-tabpanel",
      "aria-selected": key === memoryTab ? "true" : "false", tabIndex: key === memoryTab ? 0 : -1,
      onclick: () => { memoryTab = key; renderMemory(); $(`#agent-tab-${key}`).focus(); } }, title)));
  bar.addEventListener("keydown", (e) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    const at = tabs.findIndex(([key]) => key === memoryTab);
    memoryTab = tabs[(at + step + tabs.length) % tabs.length][0];
    renderMemory();
    $(`#agent-tab-${memoryTab}`).focus();
  });
  const panel = el("div", { class: "tabpanel agent-memory-panel", role: "tabpanel", id: "agent-tabpanel",
    "aria-labelledby": `agent-tab-${memoryTab}` });
  const file = FILES.find(([name]) => name === memoryTab);
  if (file) {
    const [name, , hint] = file;
    const box = el("textarea", { id: `agent-file-${name}`, rows: "18", class: "mono", "aria-label": file[1], ...PRIVATE_TYPING });
    box.value = name in drafts ? drafts[name] : (files[name] || "");
    box.addEventListener("input", () => { drafts[name] = box.value; });
    boxes[name] = box;
    const note = el("span", { class: "agent-saved", role: "status" });
    const save = el("button", { type: "button", class: "btn primary", onclick: () => saveFile(name, save, note) }, "Save");
    panel.append(el("p", { class: "fine" }, hint), box,
      el("div", { class: "agent-file-buttons" }, note, el("span", { class: "spacer" }), save));
  } else {
    panel.append(el("p", { class: "fine" }, "Skills the agent can load when a goal needs one. Only each one's name and "
      + "description go into its prompt."), el("div", { id: "agent-skills" }, skillsList()));
  }
  $("#agent-memory").replaceChildren(el("div", { class: "agent-memory-card" },
    el("header", {}, el("h2", {}, "Memory and skills"),
      el("p", { class: "fine" }, "Plain files the agent reads at the start of every run. They stay on this computer.")),
    bar, panel));
}

function skillsList() {
  const skills = (files && files.skills) || [];
  if (!skills.length) {
    return el("p", { class: "fine" }, "No skills yet. A skill is a folder with a SKILL.md in the agent's skills folder.");
  }
  return el("ul", { class: "agent-skills" }, skills.map((s) => el("li", {},
    el("strong", {}, s.name), s.description ? el("span", {}, s.description) : null)));
}

// Saves one file and shows what the server kept; the other boxes keep what you're typing
async function saveFile(name, button, note) {
  button.disabled = true;
  note.textContent = "";
  try {
    files = await putJSON("/api/agent/files", { name, text: boxes[name].value });
    boxes[name].value = files[name] || "";
    delete drafts[name];
    note.textContent = "Saved";
    setStatus("");
  } catch (e) {
    setStatus(`Couldn't save ${name}: ${e.message}`);
  } finally {
    button.disabled = false;
  }
}
