// Agent (beta): a conversation with Ixel's agent in a folder you pick, on a model you already set up. The model
// only asks for one action at a time; Ixel reads files itself and anything that changes your computer (a write,
// an edit, a command) waits here for your Approve. Everything a run holds (its goal, thoughts, results, diffs,
// file names and answer) is untrusted: it only ever becomes text nodes, never HTML. The API is in AGENT_BETA.md.

import { $, el, icon, api, getJSON, postJSON, plural, copyButton, fillIcons, PRIVATE_TYPING } from "./common.js";
import { renderMarkdown } from "./markdown.js";

const POLL_EVERY = 1000;  // a live run is read this often while the page is on screen
const ARGS_SHOWN = 80;  // characters of an action's args in a one-line summary
const ROW_SHOWN = 70;   // characters of a path or command in a step's row
const MAX_UPLOAD = 50 * 1024 * 1024;  // per file, as the server allows
const MAX_UPLOADS = 10;  // files per run
const EARLIER_TURNS = 20;  // a conversation opened from History shows at most this many runs before the last

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
const COMPUTER_WARNING = "Computer access lets the agent work outside the folder you picked: read and change files "
  + "anywhere in your home folder, run commands without the sandbox and with the network, take screenshots and open "
  + "apps, files and links. Every change, command, screenshot and open still waits for your Approve, but an approved "
  + "command can do anything you can. Turn it on only for a model and a goal you trust.";

let models = null;     // GET /api/agent/models
let runs = null;       // GET /api/agent/runs
let files = null;      // GET /api/agent/files
let prefilled = false; // the chips have been filled from the newest earlier run once
let polling = null;
let reading = false;   // a poll is still waiting for its reply
let sending = false;   // a start request is on its way

// What the next message is sent with. The page keeps nothing in browser storage: a reload starts from the
// newest run in the list.
let model = "";
let workspace = "";
let fast = false;
let computer = false;  // only ever true once you've confirmed the warning
let effort = "";
let uploads = [];      // files attached to the next message: { id, name, kind } (no id while it uploads)

// The conversation on screen: its runs oldest first (the last one is the one followed while it's live),
// and notes the page added in between (what /help says, say)
let thread = [];
let notes = [];
let noteCount = 0;
const finishedAt = new Map();  // run id → when this page saw it finish, to say how long it worked
const activityOpen = new Map();  // run id → the activity group opened or closed by you, against its default
const openSteps = new Set();   // "run id:step" whose result is showing
const turnCache = new Map();   // run id or note → { sig, node }: a poll that changed nothing redraws nothing
const boxes = {};      // the Memory panel's text boxes, by name
const drafts = {};     // what you've typed in them and not saved yet, kept while you switch tabs

export function start() {
  fillIcons($("#view-agent"));
  $("#agent-stop").addEventListener("click", (e) => stopRun(e.currentTarget));
  $("#agent-history-button").addEventListener("click", () => showHistory($("#agent-history").hidden));
  $("#agent-memory-button").addEventListener("click", () => showMemory($("#agent-memory").hidden));
  $("#agent-composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
  const box = $("#agent-box");
  box.addEventListener("keydown", onBoxKeydown);
  box.addEventListener("input", grow);
  $("#agent-attach").addEventListener("click", openPicker);
  $("#agent-upload-input").addEventListener("change", (e) => {
    addUploads([...e.currentTarget.files]);
    e.currentTarget.value = "";  // picking the same file again still counts
  });
  $("#agent-chip-folder").addEventListener("click", pickFolder);
  $("#agent-chip-model").addEventListener("click", pickModel);
  $("#agent-chip-fast").addEventListener("click", () => setFast(!fast));
  $("#agent-chip-computer").addEventListener("click", () => setComputer(!computer));
  listenForDrops();
  updateChips();
  renderUploads();
}

export function shown() {
  if (!models) loadModels();
  if (!files) loadFiles();
  loadRuns();
  renderConversation();
  updateButtons();
  if (lastRun() && isLive()) refresh().then(() => { if (isLive()) poll(); });
}

export function hidden() {
  stopPolling();
}

// ── Small pieces ──────────────────────────────────────────────────────────

const lastRun = () => thread[thread.length - 1] || null;
const isLive = () => Boolean(lastRun() && LIVE.has(lastRun().status));

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
  const at = new Date(startedMs({ started }));
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// When a run started, in milliseconds (the server sends seconds or milliseconds)
function startedMs(r) {
  const s = Number(r.started) || 0;
  return s && s < 1e12 ? s * 1000 : s;
}

function money(cost) {
  const c = Number(cost) || 0;
  if (c === 0) return "$0";
  return c < 0.01 ? `$${c.toFixed(4)}` : `$${c.toFixed(2)}`;
}

// "40 s", or "3 min 5 s" for a longer run
function duration(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

// ── Dialogs (the Board's pattern, in the Agent's own dialog) ──────────────

function showDialog(...children) {
  const dialog = $("#agent-dialog");
  dialog.replaceChildren(...children);
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue), { once: true }));
}

function dialogButtons(yes, danger) {
  const cancel = el("button", { type: "button", class: "btn" }, "Cancel");
  cancel.addEventListener("click", () => $("#agent-dialog").close("cancel"));
  return el("div", { class: "dialog-buttons" }, cancel,
    el("button", { type: "submit", class: `btn ${danger ? "danger-fill" : "primary"}`, value: "yes" }, yes));
}

// ── The chips: folder, model, Fast, Computer access ───────────────────────

function modelLabel() {
  const m = (models || []).find((x) => x.name === model);
  return m ? (m.label || m.name) : model;
}

function updateChips() {
  const folder = $("#agent-chip-folder");
  folder.querySelector("span").textContent = workspace ? clip(workspace, 40) : "Pick a folder";
  folder.setAttribute("aria-label", workspace ? `Folder: ${workspace}` : "Pick a folder");
  folder.title = workspace || "The folder it works in";
  const pick = $("#agent-chip-model");
  const label = modelLabel();
  pick.querySelector("span").textContent = label ? clip(label, 32) : (models && !models.some((m) => m.ready) ? "No model" : "Pick a model");
  pick.setAttribute("aria-label", label ? `Model: ${label}` : "Pick a model");
  const fastChip = $("#agent-chip-fast");
  fastChip.setAttribute("aria-pressed", String(fast));
  const computerChip = $("#agent-chip-computer");
  computerChip.setAttribute("aria-pressed", String(computer));
  computerChip.classList.toggle("on", computer);
}

// The folder chip: type the folder (a browser can't hand Ixel a folder's path)
async function pickFolder() {
  const input = el("input", { type: "text", id: "agent-folder-input", class: "mono", ...PRIVATE_TYPING,
    placeholder: "A folder in your home folder, like ~/code/shop" });
  input.value = workspace;
  const answer = showDialog(el("form", { method: "dialog" }, el("h2", {}, "Folder"),
    el("p", {}, "The folder the agent works in. It reads only inside it, unless Computer access is on."),
    el("div", { class: "field" }, el("label", { for: "agent-folder-input" }, "Folder"), input),
    dialogButtons("Use this folder")));
  input.focus();
  input.select();
  if ((await answer) === "yes") setFolder(input.value);
  $("#agent-box").focus();
}

// The model chip: one of the models set up in Settings
async function pickModel() {
  const list = models || [];
  const select = el("select", { id: "agent-model-input" }, list.map((m) => el("option", { value: m.name, disabled: !m.ready },
    m.ready ? (m.label || m.name) : `${m.label || m.name} (not set up)`)));
  if (!list.length) select.append(el("option", { value: "" }, models ? "No models: set one up in Settings" : "Loading the models…"));
  select.value = model;
  const answer = showDialog(el("form", { method: "dialog" }, el("h2", {}, "Model"),
    el("p", {}, "The model the agent asks for each step. Set models up in Settings."),
    el("div", { class: "field" }, el("label", { for: "agent-model-input" }, "Model"), select),
    dialogButtons("Use this model")));
  select.focus();
  if ((await answer) === "yes" && select.value) setModel(select.value);
  $("#agent-box").focus();
}

// Uses a model by its name or its label; says so in the conversation when there's none like it
function setModel(name) {
  const want = String(name || "").trim().toLowerCase();
  if (!want) { addNote("Type the model's name after /model, like /model NAME."); return; }
  const found = (models || []).find((m) => m.name.toLowerCase() === want || (m.label || "").toLowerCase() === want);
  if (!found) {
    const ready = (models || []).filter((m) => m.ready).map((m) => m.name);
    addNote(`There's no model called ${name}.${ready.length ? ` These are set up: ${ready.join(", ")}.` : " Set one up in Settings."}`);
    return;
  }
  if (!found.ready) { addNote(`${found.label || found.name} isn't set up yet: add it in Settings first.`); return; }
  model = found.name;
  updateChips();
}

function setFolder(path) {
  const folder = String(path || "").trim();
  if (!folder) { addNote("Type the folder after /folder, like /folder ~/code/shop."); return; }
  workspace = folder;
  updateChips();
}

function setFast(on) {
  fast = Boolean(on);
  updateChips();
}

// The effort passed to the model on every call of the next run; "" (or "default") leaves it to the model
function setEffort(level) {
  const value = String(level || "").trim();
  effort = value.toLowerCase() === "default" ? "" : value;
  addNote(effort ? `The next message runs at ${effort} effort.` : "The next message runs at the model's own effort.");
}

// Turning Computer access on always shows the warning first; Cancel or Escape leaves it off
async function setComputer(on) {
  if (!on) {
    computer = false;
    updateChips();
    return;
  }
  if (computer) return;
  const answer = showDialog(el("form", { method: "dialog" }, el("h2", {}, "Turn on Computer access?"),
    el("p", {}, COMPUTER_WARNING), dialogButtons("Turn on", true)));
  $("#agent-dialog .dialog-buttons .btn:first-child").focus();  // Cancel: the safe answer is the easy one
  computer = (await answer) === "yes";
  updateChips();
  $("#agent-chip-computer").focus();
}

// ── Files attached to the next message ────────────────────────────────────

function openPicker() {
  $("#agent-upload-input").click();
}

// Each file is sent to Ixel at once, one request each, and shows as a chip until the message goes
function addUploads(list) {
  for (const file of list) {
    if (uploads.length >= MAX_UPLOADS) {
      setStatus(`A message can have at most ${MAX_UPLOADS} files.`);
      break;
    }
    if (file.size > MAX_UPLOAD) {
      setStatus(`${file.name || "That file"} is bigger than 50 MB.`);
      continue;
    }
    const item = { id: "", name: file.name || "file", kind: "" };
    uploads.push(item);
    upload(item, file);
  }
  renderUploads();
}

async function upload(item, file) {
  const form = new FormData();
  form.append("file", file, item.name);
  try {
    const res = await api("/api/agent/upload", { method: "POST", body: form });  // the browser sets the multipart type
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    if (!uploads.includes(item)) return;  // taken out meanwhile
    Object.assign(item, { id: data.id, name: data.name || item.name, kind: data.kind || "" });
  } catch (e) {
    if (!uploads.includes(item)) return;
    uploads = uploads.filter((u) => u !== item);
    setStatus(`Couldn't attach ${item.name}: ${e.message}`);
  }
  renderUploads();
}

function removeUpload(item) {
  uploads = uploads.filter((u) => u !== item);
  renderUploads();
  $("#agent-box").focus();
}

function renderUploads() {
  const box = $("#agent-attachments");
  box.hidden = !uploads.length;
  box.replaceChildren(...uploads.map((u) => el("li", { class: `agent-file${u.id ? "" : " working"}` },
    icon(u.kind === "picture" || u.kind === "video" ? "image" : "file"),
    el("span", { class: "name", title: u.name }, u.name),
    u.id ? null : el("i", { class: "spin", role: "img", "aria-label": "attaching" }),
    el("button", { type: "button", class: "icon-btn", "aria-label": `Remove ${u.name}`, title: "Remove",
      onclick: () => removeUpload(u) }, icon("x")))));
}

// A file dropped anywhere on the Agent page is attached; Ask handles drops on its own page
function listenForDrops() {
  const hasFiles = (e) => Boolean(e.dataTransfer) && [...e.dataTransfer.types].includes("Files");
  const here = () => !$("#view-agent").hidden;
  document.addEventListener("dragover", (e) => {
    if (!hasFiles(e) || !here()) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    $("#agent-composer").classList.add("dropping");
  });
  document.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget) $("#agent-composer").classList.remove("dropping");
  });
  document.addEventListener("drop", (e) => {
    $("#agent-composer").classList.remove("dropping");
    if (!hasFiles(e) || !here()) return;
    e.preventDefault();
    addUploads([...e.dataTransfer.files]);
  });
}

// ── The composer ──────────────────────────────────────────────────────────

// Enter sends, Shift+Enter is a new line
function onBoxKeydown(e) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
}

// The box grows with what you type, up to a part of the window
function grow() {
  const box = $("#agent-box");
  box.style.height = "auto";  // CSSOM, not an inline style attribute
  box.style.height = `${Math.min(box.scrollHeight, Math.round(window.innerHeight * 0.4))}px`;
}

// Sends what's in the box: a new run, or a follow-up to the last one when it has finished
async function send() {
  const box = $("#agent-box");
  const goal = box.value.trim();
  if (!goal || sending) return;
  if (isLive()) { setStatus("The agent is still working on this. Wait for it, or stop it first."); return; }
  if (!model) { setStatus("Pick a model first."); pickModel(); return; }
  if (!workspace) { setStatus("Pick the folder it should work in first."); pickFolder(); return; }
  if (uploads.some((u) => !u.id)) { setStatus("Wait for the files to finish attaching."); return; }
  const body = { model, workspace, goal, fast, effort, uploads: uploads.map((u) => u.id) };
  if (computer) Object.assign(body, { computer: true, confirmed: true });
  const before = lastRun();
  if (before) body.follows = before.id;
  sending = true;
  updateButtons();
  try {
    const reply = await postJSON("/api/agent/start", body);
    setStatus("");
    box.value = "";
    grow();
    uploads = [];
    renderUploads();
    if (computer) {  // Computer access is for that run only: the next one asks again
      computer = false;
      updateChips();
    }
    await follow(reply.id);
  } catch (e) {
    setStatus(`Couldn't start the run: ${e.message}`);
  } finally {
    sending = false;
    updateButtons();
  }
}

function updateButtons() {
  const live = isLive();
  $("#agent-send").disabled = live || sending;
  $("#agent-stop").hidden = !live;
  const last = lastRun();
  $("#agent-sub").textContent = last ? [STATUS_WORDS[last.status] || last.status, modelName(last.model)].filter(Boolean).join(" · ") : "";
}

const modelName = (name) => {
  const m = (models || []).find((x) => x.name === name);
  return m ? (m.label || m.name) : name;
};

// ── The conversation ──────────────────────────────────────────────────────

// A new run joins the conversation on screen and is followed while it's live
async function follow(id) {
  showPanels(null);
  let run = null;
  try {
    run = await getJSON(`/api/agent/run?id=${encodeURIComponent(id)}`);
  } catch (e) {
    setStatus(`Couldn't read the run: ${e.message}`);
    return;
  }
  thread.push(run);
  renderConversation();
  updateButtons();
  loadRuns();
  if (isLive()) poll();
}

// Opens a conversation from History: the run picked and, before it, the runs it followed up
async function show(id) {
  stopPolling();
  showPanels(null);
  const chain = [];
  let next = id;
  while (next && chain.length <= EARLIER_TURNS) {
    try {
      chain.unshift(await getJSON(`/api/agent/run?id=${encodeURIComponent(next)}`));
    } catch (e) {
      if (!chain.length) {
        setStatus(`Couldn't read the run: ${e.message}`);
        if (isLive()) poll();  // the conversation on screen goes on
        return;
      }
      break;  // an earlier run that's gone: the conversation starts after it
    }
    next = chain[0].follows;
  }
  thread = chain;
  notes = [];
  activityOpen.clear();
  openSteps.clear();
  const last = lastRun();
  if (last.model) model = last.model;
  if (last.workspace) workspace = last.workspace;
  updateChips();
  renderConversation(true);
  updateButtons();
  if (isLive()) poll();
  $("#agent-box").focus();
}

// An empty conversation; what's attached and the chips stay as they are
function newConversation() {
  stopPolling();
  thread = [];
  notes = [];
  activityOpen.clear();
  openSteps.clear();
  showPanels(null);
  setStatus("");
  renderConversation();
  updateButtons();
  $("#agent-box").focus();
}

// A line from the page itself in the conversation, like what /help says
function addNote(text) {
  noteCount += 1;
  notes.push({ key: `note-${noteCount}`, after: thread.length, text: String(text) });
  showPanels(null);
  renderConversation(true);
}

// Reads the last run again; the page notes when it saw a run finish, to say how long it worked
async function refresh() {
  const last = lastRun();
  if (!last) return;
  const before = last.status;
  let run = null;
  try {
    run = await getJSON(`/api/agent/run?id=${encodeURIComponent(last.id)}`);
  } catch (e) {
    if (e.status === 404) thread = thread.filter((r) => r !== last);
    else setStatus(`Couldn't read the run: ${e.message}`);
    renderConversation();
    updateButtons();
    return;
  }
  if (lastRun() !== last) return;  // a new conversation began meanwhile
  replaceLast(run, before);
}

function replaceLast(run, before) {
  if (LIVE.has(before) && !LIVE.has(run.status) && !finishedAt.has(run.id)) finishedAt.set(run.id, Date.now());
  thread[thread.length - 1] = run;
  renderConversation();
  updateButtons();
  if (before !== run.status) loadRuns();  // History shows each run's status
}

function poll() {
  stopPolling();
  polling = setInterval(async () => {
    if ($("#view-agent").hidden || !lastRun()) { stopPolling(); return; }
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
  const last = lastRun();
  if (!last || !LIVE.has(last.status)) { addNote("Nothing is running."); return; }
  if (button) button.disabled = true;
  try {
    replaceLast(await postJSON("/api/agent/stop", { id: last.id }), last.status);
  } catch (e) {
    setStatus(`Couldn't stop the run: ${e.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

async function decide(approve, always, card) {
  const last = lastRun();
  for (const b of card.querySelectorAll("button")) b.disabled = true;
  try {
    replaceLast(await postJSON("/api/agent/decide", { id: last.id, approve, always }), last.status);
  } catch (e) {
    setStatus(`Couldn't send your answer: ${e.message}`);
    for (const b of card.querySelectorAll("button")) b.disabled = false;
    return;
  }
  if (isLive()) poll();
  $("#agent-box").focus();
}

// Draws the turns and notes, rebuilding only those that changed, and keeps the newest in view
function renderConversation(toBottom = false) {
  const box = $("#agent-conversation");
  const scroller = $("#view-agent .scroller");
  const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120;
  if (!thread.length && !notes.length) {
    turnCache.clear();
    box.replaceChildren(emptyState());
    return;
  }
  if (box.querySelector(".agent-hint")) box.replaceChildren();
  // Each note sits after the run it was added under (or at the end, when that run has gone)
  const notesAt = (i) => notes.filter((x) => Math.min(x.after, thread.length) === i)
    .map((n) => ({ key: n.key, sig: n.text, draw: () => note(n.text) }));
  const items = notesAt(0);
  thread.forEach((run, i) => {
    items.push({ key: run.id, sig: `${JSON.stringify(run)}|${finishedAt.get(run.id) || ""}|${i === thread.length - 1}`,
      draw: () => turn(run, i === thread.length - 1) });
    items.push(...notesAt(i + 1));
  });
  const added = items.length !== box.children.length;
  const nodes = items.map((item) => {
    const cached = turnCache.get(item.key);
    if (cached && cached.sig === item.sig) return cached.node;
    const node = item.draw();
    turnCache.set(item.key, { sig: item.sig, node });
    return node;
  });
  const changed = nodes.some((node, i) => box.children[i] !== node);
  // Nodes that didn't change stay where they are, so a focused button in an earlier turn keeps its focus
  nodes.forEach((node, i) => {
    const at = box.children[i];
    if (at !== node) box.insertBefore(node, at || null);
  });
  while (box.children.length > nodes.length) box.lastElementChild.remove();
  const keys = new Set(items.map((item) => item.key));
  for (const key of [...turnCache.keys()]) if (!keys.has(key)) turnCache.delete(key);
  if (toBottom || added || (changed && nearBottom)) scroller.scrollTop = scroller.scrollHeight;
}

// What the agent does, before the conversation has anything in it
function emptyState() {
  const point = (name, title, text) => el("li", {}, icon(name), el("div", {}, el("strong", {}, title), el("span", {}, text)));
  return el("div", { class: "agent-hint" },
    el("h2", {}, "What should the agent do?"),
    el("ul", {},
      point("file", "It reads on its own", "It lists, reads and searches files in the folder you pick, and nothing outside it."),
      point("lock", "It asks before it changes anything", "Every edit shows you a diff, and every command shows the exact line, before you approve it."),
      point("book", "It remembers", "SOUL, USER and MEMORY files and its skills live under Memory and skills, on this computer.")),
    el("p", { class: "fine" }, "Pick the folder and the model below, then type. Earlier conversations are under History."));
}

function note(text) {
  return el("p", { class: "agent-note", role: "status" }, text);
}

// One turn: your message, the agent's work as one group of short rows, an approval, the answer and its cost
function turn(run, last) {
  const live = LIVE.has(run.status);
  const parts = [you(run), activity(run, live)];
  if (last && run.status === "waiting" && run.pending) parts.push(approvalCard(run.pending, Boolean(run.computer)));
  if (run.status === "done" && run.answer) {
    parts.push(el("div", { class: "agent-answer md", "aria-label": "Answer" }, renderMarkdown(run.answer)));
  }
  if (run.status === "failed") {
    parts.push(el("div", { class: "board-notice error", role: "alert" }, icon("alert"),
      el("span", { class: "notice-lines" }, run.error || "The run failed.")));
  }
  if (run.status === "stopped") {
    parts.push(el("div", { class: "board-notice", role: "status" }, icon("stop"),
      el("span", { class: "notice-lines" }, run.error || "Stopped.")));
  }
  if (!live) {
    const usage = run.usage || {};
    parts.push(el("div", { class: "agent-after" },
      el("small", { class: "agent-usage" }, `${plural(Number(usage.calls) || 0, "model call")} · ${money(usage.cost)}`),
      run.status === "done" && run.answer ? copyButton(() => run.answer, "Copy the answer") : null));
  }
  return el("article", { class: "agent-turn", "aria-label": clip(run.goal || "A run", 60) }, parts);
}

// Your message, on the right, with what it was sent with
function you(run) {
  const names = (run.uploads || []).map((u) => el("span", { class: "agent-tag", title: u.name },
    icon(u.kind === "picture" || u.kind === "video" ? "image" : "file"), el("span", {}, clip(u.name, 40))));
  const meta = [
    run.computer ? el("span", { class: "agent-badge" }, icon("alert"), el("span", {}, "Computer access")) : null,
    ...names,
  ].filter(Boolean);
  return el("div", { class: "agent-you" },
    el("div", { class: "agent-bubble" }, run.goal || ""),
    meta.length ? el("div", { class: "agent-you-meta" }, meta) : null);
}

// The agent's work: "Working…" while it runs, open; "Worked for 40 s · 6 steps" once it's done, closed
function activity(run, live) {
  const steps = (run.steps || []).filter((s) => s.action !== "finish");  // the answer shows on its own
  const count = plural(steps.length, "step");
  let words;
  if (live) words = [el("span", { class: "spin" }), "Working…"];
  else if (finishedAt.has(run.id) && startedMs(run)) {
    words = [`Worked for ${duration(finishedAt.get(run.id) - startedMs(run))} · ${count}`];
  } else words = [`Worked · ${count}`];  // a run that finished before this page saw it: the API keeps no end time
  const open = activityOpen.has(run.id) ? activityOpen.get(run.id) : live;
  const group = el("details", { class: "agent-activity", open });
  group.addEventListener("toggle", () => {
    if (group.open === live) activityOpen.delete(run.id);  // back to how it would be anyway
    else activityOpen.set(run.id, group.open);
  });
  group.append(
    el("summary", {}, icon("chevron", "agent-chevron"), ...words),
    steps.length
      ? el("ol", { class: "agent-rows", "aria-label": "Steps" }, steps.map((s) => stepRow(run, s)))
      : el("p", { class: "agent-rows-empty" }, live ? "Thinking about the first step…" : "No steps."));
  return group;
}

const ROW_ICONS = {
  read_file: "file", list_dir: "folder", search: "code", view_file: "image", edit_file: "code", write_file: "file",
  run_command: "terminal", screenshot: "image", open: "play", remember: "book", load_skill: "book",
};

// The step as a short past-tense line: "Read greet.py", "Searched for 'TODO'", "Ran npm test"
function rowWords(step) {
  const args = step.args && typeof step.args === "object" ? step.args : {};
  const path = args.path !== undefined && args.path !== null ? clip(args.path, ROW_SHOWN) : "";
  switch (step.action) {
    case "read_file": return `Read ${path || "a file"}`;
    case "list_dir": return path && path !== "." ? `Listed ${path}` : "Listed the folder";
    case "search": return `Searched for '${clip(args.pattern || "", ROW_SHOWN)}'${path ? ` in ${path}` : ""}`;
    case "view_file": return `Looked at ${path || "a file"}`;
    case "edit_file": return `Edited ${path || "a file"}`;
    case "write_file": return `Wrote ${path || "a file"}`;
    case "run_command": return `Ran ${Array.isArray(args.argv) ? clip(commandLine(args.argv), ROW_SHOWN) : "a command"}`;
    case "screenshot": return "Took a screenshot";
    case "open": return `Opened ${clip(args.target || "", ROW_SHOWN)}`;
    case "remember": return "Remembered";
    case "load_skill": return `Loaded the skill ${clip(args.name || "", ROW_SHOWN)}`;
    default: {
      const said = argsSummary(args);
      return said ? `${step.action} ${said}` : String(step.action);
    }
  }
}

// A step's row is a button that shows its thought and result under it
function stepRow(run, step) {
  const failed = step.ok === false;
  const key = `${run.id}:${step.n}`;
  const showing = openSteps.has(key);
  const moreId = `agent-step-${String(run.id).replace(/[^\w-]/g, "")}-${step.n}`;
  const hasResult = step.result !== undefined && step.result !== null && step.result !== "";
  const pictures = Number(step.pictures) || 0;
  const more = el("div", { class: "agent-row-more", id: moreId, hidden: !showing },
    step.thought ? el("p", { class: "agent-row-thought" }, step.thought) : null,
    hasResult ? el("pre", { class: failed ? "failed" : "ok" }, String(step.result)) : null,
    !step.thought && !hasResult ? el("p", { class: "agent-row-thought" }, "Nothing more to show.") : null);
  const head = el("button", { type: "button", class: "agent-row-head", "aria-expanded": String(showing), "aria-controls": moreId },
    icon(failed ? "alert" : (ROW_ICONS[step.action] || "circle")),
    el("span", { class: "agent-row-text" }, rowWords(step), pictures ? ` · ${plural(pictures, "picture")}` : ""),
    failed ? el("span", { class: "sr-only" }, " (failed)") : null);
  head.addEventListener("click", () => {
    const open = more.hidden;
    more.hidden = !open;
    head.setAttribute("aria-expanded", String(open));
    if (open) openSteps.add(key); else openSteps.delete(key);
  });
  return el("li", { class: `agent-row ${failed ? "failed" : "ok"}` }, head, more);
}

// What the run waits for: the change or the exact command, and your answer
function approvalCard(pending, computerOn) {
  const command = pending.action === "run_command";
  const args = pending.args || {};
  const card = el("section", { class: "agent-approval", "aria-label": "Waiting for your approval" });
  const body = [];
  if (command) {
    const argv = Array.isArray(pending.argv) && pending.argv.length ? pending.argv : (args.argv || []);
    body.push(el("p", { class: "agent-approval-what" }, "It wants to run this command:"),
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
  if (command && !computerOn) {
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

// ── History and Memory, behind their buttons in the top bar ───────────────

// At most one panel is open, and while it is the conversation steps aside; null closes both
function showPanels(which) {
  for (const [name, button] of [["history", "#agent-history-button"], ["memory", "#agent-memory-button"]]) {
    const on = which === name;
    $(`#agent-${name}`).hidden = !on;
    $(button).setAttribute("aria-expanded", on ? "true" : "false");
    $(button).classList.toggle("on", on);
  }
  $("#agent-conversation").hidden = Boolean(which);
}

function showHistory(on) {
  showPanels(on ? "history" : null);
  if (on) {
    renderHistory();
    loadRuns();
  }
}

// Memory and skills take the place of the conversation while they're open, so each file gets the room to edit it
function showMemory(on) {
  showPanels(on ? "memory" : null);
  if (on && files) renderMemory();
}

async function loadModels() {
  try {
    const data = await getJSON("/api/agent/models");
    models = data.models || [];
  } catch (e) {
    setStatus(`Couldn't list the models: ${e.message}`);
    return;
  }
  if (!model || !models.some((m) => m.ready && m.name === model)) {
    const ready = models.find((m) => m.ready);
    model = ready ? ready.name : "";
  }
  updateChips();
  updateButtons();
  prefill();
}

async function loadRuns() {
  try {
    const data = await getJSON("/api/agent/runs");
    runs = data.runs || [];
  } catch (e) {
    setStatus(`Couldn't list the earlier runs: ${e.message}`);
  }
  if (!$("#agent-history").hidden) renderHistory();
  prefill();
}

// Once the models and the run list are both in, take the folder and model of the newest earlier run and
// reopen a run that is still going. Only the first time, so it never overwrites what you pick.
function prefill() {
  if (prefilled || !models || !runs) return;
  prefilled = true;
  if (!runs.length) return;
  const newest = runs.reduce((a, b) => (startedMs(b) > startedMs(a) ? b : a));
  if (!workspace && newest.workspace) workspace = newest.workspace;
  const same = models.find((m) => m.ready && m.name === newest.model);
  if (same) model = same.name;
  updateChips();
  const live = runs.find((r) => LIVE.has(r.status));
  if (live && !thread.length) show(live.id);
}

// Earlier conversations, newest first: each run that no later run follows up
function renderHistory() {
  const box = $("#agent-history");
  const head = el("header", {}, el("h2", {}, "History"), el("span", { class: "spacer" }),
    el("button", { type: "button", class: "btn small", onclick: newConversation }, icon("plus"), el("span", {}, "New conversation")));
  if (!runs) { box.replaceChildren(el("div", { class: "agent-panel-card" }, head, el("div", { class: "set-loading" }, el("span", { class: "spin" }), "Looking…"))); return; }
  const followed = new Set(runs.map((r) => r.follows).filter(Boolean));
  const ends = runs.filter((r) => !followed.has(r.id)).sort((a, b) => startedMs(b) - startedMs(a));
  const current = lastRun() ? lastRun().id : "";
  box.replaceChildren(el("div", { class: "agent-panel-card" }, head, ends.length
    ? el("ul", { class: "agent-run-list" }, ends.map((r) => el("li", {},
      el("button", { type: "button", class: "agent-run-pick", "aria-current": r.id === current ? "true" : null,
        onclick: () => show(r.id) },
      el("span", { class: "agent-run-goal" }, clip(r.goal || "(no goal)", 120)),
      el("span", { class: "agent-run-when" },
        el("span", { class: `agent-state s-${r.status}` }, STATUS_WORDS[r.status] || r.status),
        r.workspace ? ` · ${r.workspace}` : "",
        when(r.started) ? ` · ${when(r.started)}` : "")))))
    : el("p", { class: "fine" }, "None yet. Each conversation you start is kept here.")));
}

// ── Memory ────────────────────────────────────────────────────────────────

async function loadFiles() {
  try {
    files = await getJSON("/api/agent/files");
  } catch (e) {
    setStatus(`Couldn't read the agent's memory: ${e.message}`);
    $("#agent-memory").replaceChildren(el("div", { class: "agent-memory-card" }, el("h2", {}, "Memory and skills"),
      el("p", { class: "fine" }, "Not loaded.")));
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
    const saved = el("span", { class: "agent-saved", role: "status" });
    const save = el("button", { type: "button", class: "btn primary", onclick: () => saveFile(name, save, saved) }, "Save");
    panel.append(el("p", { class: "fine" }, hint), box,
      el("div", { class: "agent-file-buttons" }, saved, el("span", { class: "spacer" }), save));
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
async function saveFile(name, button, saved) {
  button.disabled = true;
  saved.textContent = "";
  try {
    files = await putJSON("/api/agent/files", { name, text: boxes[name].value });
    boxes[name].value = files[name] || "";
    delete drafts[name];
    saved.textContent = "Saved";
    setStatus("");
  } catch (e) {
    setStatus(`Couldn't save ${name}: ${e.message}`);
  } finally {
    button.disabled = false;
  }
}
