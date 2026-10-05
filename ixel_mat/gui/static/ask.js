// Ask: put a question to the panel (and /handoff, which splits a request across agents).
// Everything a model (or the server) sends is untrusted: it is only ever inserted as text nodes,
// never as HTML.

import {
  $, $$, el, icon, fillIcons, secs, plural, copyButton, api, rememberedProject, rememberProject, postJSON,
} from "./common.js";
import { inline, renderMarkdown } from "./markdown.js";
import * as pics from "./pictures.js";
import * as sound from "./sound.js";
import * as video from "./video.js";

// ── Words ─────────────────────────────────────────────────────────────────

const MODE_TITLES = { quick: "Quick", review: "Review", deep: "Deep", saver: "Saver", auto: "Auto" };
const MODE_HINTS = {
  quick: "Everyone answers on their own, then a moderator writes one verdict.",
  review: "Adds anonymous peer review: the models grade each other and flag errors.",
  deep: "Adds a revision round: each model fixes its answer after the critiques.",
  auto: "Triage reads your question first and picks Quick, Review or Deep.",
};
const MODE_ROUNDS = {
  quick: ["answer", "verdict"], review: ["answer", "review", "verdict"],
  deep: ["answer", "review", "revise", "verdict"], saver: ["answer", "review", "verify"],
  auto: ["answer", "review", "verdict"],  // until Triage has picked
};
const ROUND_TITLES = {
  answer: "Answer", review: "Peer review", revise: "Revise", verdict: "Verdict", verify: "Big-model check", fix: "Fix",
};
const WORKING = {
  answer: "answering", review: "reviewing", revise: "revising", verdict: "writing", verify: "verifying", fix: "fixing",
};
const OUTCOMES = {
  confirmed: "confirmed a draft", corrected: "corrected the drafts", unresolved: "still found problems",
  answered: "answered directly", failed: "failed",
};
const VERDICT_GLYPHS = { correct: "✓", partially_correct: "~", incorrect: "✗", unsure: "?" };
const CONFIDENCE = { high: 3, medium: 2, low: 1 };

// ── State ─────────────────────────────────────────────────────────────────

let panel = {
  agents: [], review: { mode: "review" }, saver: { verifier: null, drafters: [] }, triage: {}, version: "", warnings: [],
};
let mode = "review";
let modePicked = false;  // the person chose a mode here, rather than taking the default
// A follow-up carries the last few questions and the panel's answers to them
const MAX_EARLIER = 3;
const EARLIER_CHARS = 8000;  // the server clips them further, and marks what it cut
const MAX_CODE_CHARS = 60000;  // material.MAX_MATERIAL_CHARS: every model reads it in every round
const MAX_QUESTION_CHARS = 50000;  // the server's MAX_QUESTION_CHARS
const TOO_LONG = (n) => `Your question is ${n.toLocaleString("en-US")} characters, over the ` +
  `${MAX_QUESTION_CHARS.toLocaleString("en-US")} a question can be. Shorten it before you ask.`;
const TOO_MUCH_CODE = (n) => `That's ${n.toLocaleString("en-US")} characters of code, and the panel takes up to ` +
  `${MAX_CODE_CHARS.toLocaleString("en-US")}: every model reads it in every round. Narrow it down, for example ` +
  "to the files that matter.";
const topics = [];     // this tab's conversations, oldest first
let active = null;     // the conversation on screen; null is a new one
let current = null;    // the run in progress: { turn, controller }
let nextId = 1;

const scroller = $("#scroller");

// ── Notices and toasts ────────────────────────────────────────────────────

let panelNotice = false;  // the notice on screen is about the panel (no models, or the settings' warnings)

function showNotice(message, kind = "info") {
  panelNotice = false;
  const box = $("#notice");
  box.className = `notice ${kind}`;
  box.replaceChildren(icon(kind === "error" ? "alert" : "info"),
    el("span", {}, ...(Array.isArray(message) ? message : [message])));
  box.hidden = false;
}

const hideNotice = () => { $("#notice").hidden = true; };

function toast(title, text) {
  const box = el("div", { class: "toast", title: "Click to dismiss" }, icon("trophy"),
    el("div", {}, el("b", {}, title), text ? el("span", {}, text) : null));
  box.addEventListener("click", () => box.remove());
  $("#toasts").append(box);
  setTimeout(() => box.remove(), 6000);
}

// ── Panel (sidebar) ───────────────────────────────────────────────────────

async function loadPanel() {
  let res;
  try {
    res = await api("/api/panel");
  } catch (e) {
    showNotice("Can't reach Ixel. Is `ixel gui` still running in your terminal?", "error");
    return;
  }
  if (res.status === 401) {
    showNotice(["Open this page with the link ", el("code", {}, "ixel gui"),
      " printed in your terminal. It carries a one-time key."], "error");
    $("#ask").disabled = true;
    return;
  }
  panel = await res.json();
  panel.triage = panel.triage || {};
  $("#version").textContent = `v${panel.version}`;
  $('.modes button[data-mode="auto"]').hidden = !panel.triage.auto;
  mode = panel.review.mode || "review";
  if (mode === "auto" && !panel.triage.auto) mode = "review";
  renderPanel();
  renderModes();
  renderTray();
  renderSound();
  loadSaves();
  showPanelNotice(true);
}

// "No models yet", or what's wrong in the settings; `first` is the page opening, when any notice can go
function showPanelNotice(first) {
  if (!first && !panelNotice && !$("#notice").hidden) return;  // something else is being said: leave it
  if (!panel.agents.length && panel.private && panel.private.on) {
    showNotice("Private is on, and none of your models runs on your own computers. Add one in Settings, under " +
      "Models on your computers, or turn Private off there.", "info");
  } else if (!panel.agents.length) {
    showNotice(["No models are set up yet. In a terminal, run ", el("code", {}, "ixel setup"), "."], "info");
  } else if (panel.warnings.length) {
    showNotice(panel.warnings.join(" · "), "info");
  } else {
    if (panelNotice) hideNotice();
    return;
  }
  panelNotice = true;
}

let panelAsked = 0;     // the newest look at the panel wins
let panelStale = false; // the settings changed while a question ran: looked at again once it's done

// After a change on the Settings page: the panel as it is now. A mode picked here stays picked, and a
// question under way keeps the panel it started with.
export async function settingsChanged() {
  if (current) {
    panelStale = true;
    return;
  }
  const mine = ++panelAsked;
  let fresh;
  try {
    const res = await api("/api/panel");
    if (!res.ok) return;
    fresh = await res.json();
  } catch (e) {
    return;  // the next look gets it
  }
  if (mine !== panelAsked || current) {
    if (current) panelStale = true;
    return;
  }
  panel = fresh;
  panel.triage = panel.triage || {};
  $('.modes button[data-mode="auto"]').hidden = !panel.triage.auto;
  if (!modePicked) mode = panel.review.mode || "review";
  if (mode === "auto" && !panel.triage.auto) mode = "review";
  renderPanel();
  renderModes();
  renderTray();
  renderSound();
  renderFollowup();
  showPanelNotice(false);
}

const readyCount = () => panel.agents.filter((a) => a.ready).length;
const modelOf = (name) => (panel.agents.find((a) => a.name === name) || {}).model || "";

function renderPanel() {
  const saver = panel.saver || {};
  $("#panel-count").textContent = panel.agents.length ? `${readyCount()}/${panel.agents.length} ready` : "";
  $("#panel").replaceChildren(...panel.agents.map((a) => {
    const roles = [];
    if (panel.review.moderator && panel.review.moderator === a.name) roles.push("moderator");
    if (saver.verifier && saver.verifier === a.label) roles.push("verifier");
    return el("li", { class: `model ${a.ready ? "ready" : "not-ready"}`, title: [a.type, a.model].filter(Boolean).join(" · ") },
      el("i", { class: "dot", "aria-hidden": "true" }),
      el("span", { class: "name" }, a.label,
        a.pictures ? el("span", { class: "sees", role: "img", title: "Sees the pictures you attach", "aria-label": "sees pictures" },
          icon("image")) : null),
      el("span", { class: "roles" }, roles.map((r) => el("span", { class: "role" }, r))),
      el("span", { class: "id" }, a.ready ? a.model || a.type : "missing API key"));
  }));
  const priv = panel.private || {};
  if (priv.on) {
    const out = priv.sitting_out || [];
    $("#panel").prepend(el("li", {
      class: "model private ready",
      title: "Private: only models on your own computers answer, so questions, answers and sound stay with you." +
        (out.length ? ` Sitting out: ${out.join(", ")}.` : ""),
    },
    el("i", { class: "dot", "aria-hidden": "true" }),
    el("span", { class: "name" }, icon("lock"), "Private"),
    el("span", { class: "roles" }),
    el("span", { class: "id" }, out.length ? `${out.length} sit${out.length === 1 ? "s" : ""} out` : "only yours")));
  }
  if (!panel.agents.length) {
    $("#panel").append(el("li", { class: "none" }, priv.on ? "None on your computers yet" : "No models yet: run ixel setup"));
  }
  const triage = panel.triage || {};
  if (triage.ready) {
    const uses = [triage.auto && "auto", triage.skip_review && "skip review", triage.saver_gate && "saver"].filter(Boolean);
    const own = triage.provider === "model";
    $("#panel").append(el("li", {
      class: `model triage ${own || triage.official ? "ready" : "warn"}`,
      title: own ? `Triage: ${triage.via}, one of your models, decides how much checking each question needs.`
        : triage.official ? "Triage: quick decisions between rounds, by TypeSafe's decision API. Questions and answers go to TypeSafe."
          : `Triage requests go to ${triage.host}, not TypeSafe's own API.`,
    },
    el("i", { class: "dot", "aria-hidden": "true" }),
    el("span", { class: "name" }, icon("zap"), "Triage"),
    el("span", { class: "roles" }, uses.map((u) => el("span", { class: "role" }, u))),
    el("span", { class: "id" }, own ? `decided by ${triage.via}` : triage.official ? triage.host
      : `not TypeSafe: ${triage.host}`)));
  }
}

function saverHint() {
  const s = panel.saver || {};
  if (!s.verifier) {
    return "Saver mode needs a big model to verify: set [saver] verifier in your config, or run ixel setup.";
  }
  const n = (s.drafters || []).length;
  const skip = s.escalate === "disagreement" ? " It isn't called at all when every draft is rated correct." : "";
  return `${n} cheaper model${n === 1 ? "" : "s"} draft and check each other; ${s.verifier} only verifies ` +
    `(1 call, usually a few words).${skip}`;
}

function renderModes() {
  for (const button of $$(".modes button")) {
    const on = button.dataset.mode === mode;
    button.classList.toggle("on", on);
    button.setAttribute("aria-checked", on ? "true" : "false");
    button.tabIndex = on ? 0 : -1;
  }
  const n = readyCount();
  if (mode === "saver") {
    $("#mode-hint").textContent = saverHint();
  } else if (mode === "auto") {
    const t = panel.triage || {};
    $("#mode-hint").textContent = MODE_HINTS.auto + (t.provider === "model"
      ? ` ${t.via} decides.` : " Your question goes to TypeSafe too.");
  } else {
    const calls = n ? n * { quick: 1, review: 2, deep: 3 }[mode] + 1 : 0;
    $("#mode-hint").textContent = MODE_HINTS[mode] + (n ? ` ${n} models, about ${calls} model calls.` : "");
  }
  renderEmpty();
}

// The empty page explains what the chosen mode is about to do
function renderEmpty() {
  const n = readyCount() || panel.agents.length;
  const s = panel.saver || {};
  const drafters = (s.drafters || []).length;
  let lede;
  let steps;
  if (mode === "auto") {
    lede = "Triage reads your question first and picks how much checking it needs.";
    steps = [["Triage", "picks the depth, in about a second"], ["Answer", n ? `${plural(n, "model")}, in parallel` : "in parallel"],
      ["Review", "only if Triage thinks it's needed"], ["Verdict", "one model writes it"]];
  } else if (mode === "saver") {
    lede = s.verifier
      ? `${plural(drafters, "cheaper model")} draft and cross-check; ${s.verifier} only verifies their work.`
      : "Saver mode needs a big model to verify: set [saver] verifier in your config, or run ixel setup.";
    steps = [["Draft", `${plural(drafters, "cheaper model")}, in parallel`], ["Cross-check", "they grade each other"],
      ["Verify", s.verifier ? `${s.verifier}, a few words` : "needs a verifier"]];
  } else {
    const models = n ? plural(n, "model") : "Your models";
    lede = {
      quick: `${models} answer on their own, then one writes the verdict.`,
      review: `${models} answer on their own, grade each other anonymously, then one writes the verdict.`,
      deep: `${models} answer, grade each other anonymously, fix their answers, then one writes the verdict.`,
    }[mode];
    const sub = {
      answer: n ? `${plural(n, "call")}, in parallel` : "in parallel", review: "anonymous; self-grades don't count",
      revise: "each fixes its own answer", verdict: "one model writes it",
    };
    steps = MODE_ROUNDS[mode].map((r) => [ROUND_TITLES[r], sub[r]]);
  }
  $("#empty-lede").textContent = lede;
  $("#flow").replaceChildren(...steps.map(([title, sub], i) =>
    el("li", {}, el("span", { class: "n" }, String(i + 1)), el("b", {}, title), el("small", {}, sub))));
}

// ── Saves counter ─────────────────────────────────────────────────────────

function showSaves(s, bump) {
  $("#saver-box").hidden = false;
  const badge = $("#saves");
  badge.replaceChildren(icon("trophy"), el("span", {}, plural(s.saves, "save")));
  badge.title = `Usage saver: ${s.saves} of ${s.runs} runs answered by your cheaper models` +
    (s.streak ? `, ${s.streak} in a row` : "");
  const rate = s.runs ? ` · ${Math.round((s.saves / s.runs) * 100)}%` : "";
  $("#saves-sub").textContent = `${plural(s.runs, "saver run")}${rate}${s.streak ? ` · streak ${s.streak}` : ""}`;
  const meter = $("#saves-meter");
  meter.hidden = !s.next_milestone || !s.runs;
  if (s.next_milestone) {
    meter.title = `${s.saves} of ${s.next_milestone} for the next milestone`;
    meter.firstChild.style.width = `${Math.min(100, (s.saves / s.next_milestone) * 100)}%`;  // CSSOM, not an inline style
  }
  if (bump) {
    badge.classList.remove("bump");
    void badge.offsetWidth;  // restart the animation
    badge.classList.add("bump");
  }
}

async function loadSaves(bump = false) {
  try {
    const res = await api("/api/saves");
    if (!res.ok) return;
    const s = await res.json();
    if (s.runs || (panel.saver && panel.saver.verifier)) showSaves(s, bump);
  } catch (e) { /* the counter is a nicety */ }
}

// ── Conversations ─────────────────────────────────────────────────────────

const STORE_KEY = "ixel-conversations";

function newTopic(question) {
  const title = question.split("\n").find((l) => l.trim()) || question;
  const topic = { id: nextId++, title: title.trim().slice(0, 120), turns: [], thread: el("div", { class: "thread" }) };
  topics.push(topic);
  $("#threads").append(topic.thread);
  return topic;
}

const privateOn = () => Boolean(panel.private && panel.private.on);

// A conversation asked with Private on stays with your own models: with Private off, a follow-up starts fresh
const keptPrivate = (topic) => Boolean(topic) && !privateOn() && topic.turns.some((t) => t.private);

// The earlier questions a follow-up in this conversation carries
function earlierFor(topic) {
  if (!topic || keptPrivate(topic)) return [];
  return topic.turns.filter((t) => t.result && t.result.final && t.result.final.answer)
    .slice(-MAX_EARLIER)
    .map((t) => ({
      question: t.pictureCount ? `[${plural(t.pictureCount, "picture")} came with this question. They aren't ` +
        `sent again.]\n\n${t.result.question}` : t.result.question,
      answer: t.result.final.answer,
      private: Boolean(t.private),
    }));
}

function show(topic) {
  active = topic;
  for (const t of topics) t.thread.hidden = t !== topic;
  $("#empty").hidden = Boolean(topic);
  $("#convo-title").textContent = topic ? topic.title : "New conversation";
  document.title = topic ? `${topic.title} · Ixel MAT` : "Ixel MAT";
  renderFollowup();
  renderConvos();
  closeDrawer();
  const last = topic && topic.turns[topic.turns.length - 1];
  if (last) reveal(last.el);
  else scroller.scrollTop = 0;
}

function startOver() {
  show(null);
  $("#question").focus();
}

function renderConvos() {
  const list = $("#convos");
  if (!topics.length) {
    list.replaceChildren(el("li", { class: "none" }, "Your questions this session show up here."));
    return;
  }
  list.replaceChildren(...[...topics].reverse().map((t) => {
    const running = current && current.turn.topic === t;
    const button = el("button", { type: "button", "aria-current": t === active ? "true" : "false", title: t.title },
      running ? el("i", { class: "spin", "aria-label": "running" }) : icon("message"),
      el("span", { class: "title" }, t.title),
      t.turns.length > 1 ? el("span", { class: "n" }, String(t.turns.length)) : null);
    button.addEventListener("click", () => show(t));
    return el("li", {}, button);
  }));
}

function renderFollowup() {
  const n = earlierFor(active).length;
  $("#followup").hidden = n === 0 && !keptPrivate(active);
  $("#followup-text").textContent = keptPrivate(active)
    ? "This conversation was asked with Private on, so the panel won't see it now. A question starts a new one."
    : n === 1
    ? "Follow-up: the panel also sees your last question and its answer."
    : `Follow-up: the panel also sees your last ${n} questions and their answers.`;
  $("#question").placeholder = n
    ? "Ask a follow-up…"
    : "Ask the panel anything. Paste code, an error, or a decision you're weighing.";
}

// This tab keeps its conversations across a reload (sessionStorage is per tab
// and dies with it). Only finished turns are kept; they render as text like
// everything else.
function save() {
  const data = topics.map((t) => ({
    title: t.title, turns: t.turns.filter((turn) => turn.finished).map((turn) => turn.snapshot()),
  })).filter((t) => t.turns.length);
  while (data.length) {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify(data));
      return;
    } catch (e) {
      data.shift();  // over the quota (or storage is off): drop the oldest conversation
    }
  }
  try { sessionStorage.removeItem(STORE_KEY); } catch (e) { /* storage is off */ }
}

function restore() {
  let data;
  try { data = JSON.parse(sessionStorage.getItem(STORE_KEY) || "[]"); } catch (e) { return; }
  if (!Array.isArray(data)) return;
  for (const saved of data) {
    if (!saved || typeof saved.title !== "string" || !Array.isArray(saved.turns)) continue;
    const topic = newTopic(saved.title);
    try {
      for (const snap of saved.turns) {
        const turn = Turn.restore(topic, snap);
        topic.turns.push(turn);
        topic.thread.append(turn.el);
      }
    } catch (e) { /* a damaged entry keeps what came back before it */ }
    if (!topic.turns.length) {
      topics.splice(topics.indexOf(topic), 1);
      topic.thread.remove();
    }
  }
}

// ── A question and the panel's reply ──────────────────────────────────────

class Turn {
  constructor(topic, question, runMode, followup, shots = []) {
    this.topic = topic;
    this.question = question;
    this.shots = shots;               // { url } for each picture sent with it ("" once the page reloads)
    this.pictureCount = shots.length;
    this.mode = runMode;
    this.followup = followup;
    this.private = false;      // asked with Private on: a follow-up with it off starts fresh
    this.rounds = MODE_ROUNDS[runMode].slice();
    this.at = -1;
    this.agents = new Map();   // name → { name, label, model, letter, note, failed, cells: [] }
    this.result = null;
    this.done = false;         // the panel's final result arrived
    this.finished = false;     // nothing more will happen
    this.note = "";
    this.noteKind = "";
    this.draftText = "";
    this.tab = "";
    this.skipped = new Set();  // rounds Triage said could be left out
    this.build();
  }

  build() {
    this.statusNode = el("span", { class: "status", "aria-live": "polite" });
    this.gridNode = el("div", { class: "activity" });
    this.toggle = el("button", { type: "button", class: "icon-btn toggle", "aria-expanded": "true",
      title: "Show or hide each model's progress" }, icon("chevron"));
    this.toggle.addEventListener("click", () => this.setExpanded(this.gridNode.hidden));
    this.slot = el("div", { class: "result" });
    this.triageNode = el("ul", { class: "triage-notes", "aria-label": "Triage" });
    this.modeTag = el("span", { class: "mode-tag" }, MODE_TITLES[this.mode] || this.mode);
    const bubble = el("div", { class: "bubble" });
    if (/^\s*(```|~~~)/m.test(this.question)) bubble.append(renderMarkdown(this.question));
    else bubble.append(this.question);
    bubble.classList.toggle("md", bubble.childElementCount > 0);
    this.el = el("article", { class: "turn running" },
      el("div", { class: "you" }, this.shots.length ? this.shotsNode() : null, bubble),
      el("div", { class: "reply" },
        el("div", { class: "reply-head" },
          el("span", { class: "avatar" }, el("img", { src: "/mark.svg", alt: "", width: "16", height: "16" })),
          el("span", { class: "who" }, "Panel"),
          this.modeTag,
          this.followup ? el("span", { class: "mode-tag followup-tag" }, "follow-up") : null,
          this.statusNode,
          el("span", { class: "spacer" }),
          this.toggle),
        this.gridNode,
        this.triageNode,
        this.slot));
    if (this.question.length > 600 || this.question.split("\n").length > 10) {
      bubble.classList.add("clamped");
      const more = el("button", { type: "button", class: "linkish more" }, "Show all");
      more.addEventListener("click", () => {
        const open = bubble.classList.toggle("clamped");
        more.textContent = open ? "Show all" : "Show less";
      });
      bubble.after(more);
    }
    this.setStatus("Connecting to the panel…", true);
    this.renderGrid();
  }

  // The pictures sent with the question: small, and bigger while the button is pressed
  shotsNode() {
    const kept = this.shots.filter((p) => p.url);
    if (!kept.length) {
      return el("div", { class: "shots" }, el("span", { class: "tag" }, icon("image"),
        `${plural(this.shots.length, "picture")} (not kept after a reload)`));
    }
    const button = el("button", { type: "button", class: "shots", "aria-pressed": "false",
      title: "Show bigger", "aria-label": `${plural(kept.length, "picture")} sent with this question. Show bigger` },
    kept.map((p, i) => el("img", { src: p.url, alt: `Picture ${i + 1}`, width: String(p.width), height: String(p.height) })));
    button.addEventListener("click", () => {
      const open = button.getAttribute("aria-pressed") !== "true";
      button.setAttribute("aria-pressed", String(open));
      button.title = open ? "Show smaller" : "Show bigger";
      button.setAttribute("aria-label", `${plural(kept.length, "picture")} sent with this question. ${button.title}`);
    });
    return button;
  }

  setExpanded(open) {
    this.gridNode.hidden = !open;
    this.toggle.setAttribute("aria-expanded", open ? "true" : "false");
    this.toggle.classList.toggle("closed", !open);
  }

  setStatus(text, working = false) {
    const label = el("span", { class: "status-text" }, text);
    this.statusNode.replaceChildren(...(working ? [el("i", { class: "spin", "aria-hidden": "true" }), label] : [label]));
  }

  agent(name, label) {
    if (!this.agents.has(name)) {
      this.agents.set(name, { name, label, model: modelOf(name), letter: "", note: "", failed: false, cells: [] });
    }
    return this.agents.get(name);
  }

  byLabel(label) {
    return [...this.agents.values()].find((a) => a.label === label);
  }

  // The column an event's round belongs to (a sent-back run has verify twice)
  column(round) {
    return this.rounds[this.at] === round ? this.at : this.rounds.lastIndexOf(round);
  }

  cell(a, index) {
    if (index < 0) return {};
    if (!a.cells[index]) a.cells[index] = { state: "queued", text: "", title: "", ms: null, started: 0 };
    return a.cells[index];
  }

  finishCell(a, round, text, title) {
    if (!a) return;
    const c = this.cell(a, this.column(round));
    const ms = c.started ? performance.now() - c.started : null;
    Object.assign(c, { state: "done", text: text || "", title: title || "", ms });
  }

  handle(kind, data) {
    switch (kind) {
      case "connect": {
        const a = this.agent(data.agent, data.agent_label);
        if (!data.ok) Object.assign(a, { failed: true, note: `couldn't connect: ${data.error}` });
        break;
      }
      case "start":
        this.rounds = data.rounds.slice();
        this.mode = data.mode;
        this.auto = Boolean(data.auto);
        if (data.private) this.private = true;  // as the server ran it, whatever this page last saw
        this.modeTag.textContent = this.auto ? `Auto · ${MODE_TITLES[data.mode] || data.mode}` : MODE_TITLES[data.mode] || data.mode;
        break;
      case "triage":
        this.addTriageNote(data);
        break;
      case "round": {
        if (data.total > this.rounds.length) this.rounds.push("fix", "verify");  // sent back once
        this.at = data.number - 1;
        const taking = new Set(data.agents);
        for (const a of this.agents.values()) if (taking.has(a.label)) this.cell(a, this.at);
        if (data.round === "verify" && !data.agents.length) {
          const verifier = this.byLabel((panel.saver || {}).verifier);
          if (verifier) Object.assign(this.cell(verifier, this.at), { state: "skipped", text: "not needed" });
        }
        this.setStatus(`Round ${data.number} of ${this.rounds.length} · ${ROUND_TITLES[data.round] || data.round}`, true);
        break;
      }
      case "agent_started": {
        const c = this.cell(this.agent(data.agent, data.agent_label), this.column(data.round));
        Object.assign(c, { state: "working", started: performance.now(), text: "", ms: null });
        break;
      }
      case "answer": {
        const a = this.agent(data.agent, data.agent_label);
        this.finishCell(a, "answer", "");
        this.cell(a, this.column("answer")).ms = data.latency_ms;
        break;
      }
      case "labels":
        for (const [letter, info] of Object.entries(data.labels)) {
          if (this.agents.has(info.agent)) this.agents.get(info.agent).letter = letter;
        }
        break;
      case "agent_failed": {
        const a = this.agent(data.agent, data.agent_label);
        const c = this.cell(a, this.column(data.round));
        Object.assign(c, { state: "failed", text: data.error.startsWith("left out") ? "left out" : "failed", title: data.error });
        break;
      }
      case "review": {
        const own = data.best && data.best === data.own_label ? " (own)" : "";
        this.finishCell(this.agent(data.reviewer, data.reviewer_label), "review",
          data.best ? `picked ${data.best}${own}` : "reviewed", data.summary || "");
        break;
      }
      case "revision":
        this.finishCell(this.agent(data.agent, data.agent_label), this.rounds[this.at], this.rounds[this.at] === "fix" ? "fixed" : "revised");
        break;
      case "sent_back":
        this.finishCell(this.byLabel(data.verifier_label), "verify", "sent back", data.issues.join("; "));
        break;
      case "verified":
        if (data.outcome !== "failed") {
          this.finishCell(this.byLabel(data.verifier_label), "verify",
            data.outcome === "confirmed" && data.accepted ? `confirmed ${data.accepted}` : OUTCOMES[data.outcome] || "");
        }
        break;
      case "verdict_text":
        this.appendDraft(data.text);
        return;  // the grid hasn't changed
      case "final":
        this.complete(data.result);
        break;
      case "error":
        this.fail(data.message);
        if (data.code === "private_off") panelStale = true;  // looked at again once this is done
        break;
      default:
        return;
    }
    this.renderGrid();
  }

  stepState(i) {
    if (this.skipped.has(this.rounds[i])) return "skipped";
    if (i < this.at) return "done";
    if (i > this.at) return "";
    if (this.done && this.result && this.result.final) return "done";
    return this.finished ? "" : "active";
  }

  renderGrid() {
    const cols = this.rounds;
    const now = performance.now();
    const head = el("tr", {}, el("th", { scope: "col", class: "who-col" }, "Model"),
      cols.map((name, i) => {
        const state = this.stepState(i);
        return el("th", { scope: "col", class: `step ${state}` }, el("span", { class: "step-in" },
          el("span", { class: "n" }, state === "done" ? icon("check") : state === "skipped" ? icon("skip") : String(i + 1)),
          el("span", { class: "step-title" }, ROUND_TITLES[name] || name)));
      }));
    const rows = [...this.agents.values()].map((a) => el("tr", { class: a.failed ? "failed" : "" },
      el("th", { scope: "row" }, el("div", { class: "agent-cell" },
        el("span", { class: `letter${a.letter ? "" : " none"}`, title: a.letter ? `Answer ${a.letter}` : null }, a.letter || "·"),
        el("span", { class: "who" }, el("span", { class: "name" }, a.label),
          el("span", { class: "id", title: a.note || a.model }, a.note || a.model)))),
      cols.map((round, i) => el("td", {}, this.cellNode(a.cells[i], round, now)))));
    if (!rows.length) {
      // Refused before any model started (Private with none of your own, say): the notice above says why
      const over = this.finished || this.noteKind === "error";
      rows.push(el("tr", {}, el("td", { class: "waiting", colspan: String(cols.length + 1) },
        over ? "No model started." : "Starting the models…")));
    }
    this.gridNode.replaceChildren(el("div", { class: "grid-wrap" },
      el("table", { class: "grid" }, el("thead", {}, head), el("tbody", {}, rows))));
  }

  cellNode(c, round, now) {
    if (!c) return el("span", { class: "cell idle" }, "–");
    switch (c.state) {
      case "working": {
        const t = el("span", { class: "t" }, secs(now - c.started));
        c.timer = t;
        return el("span", { class: "cell working" }, el("i", { class: "spin", "aria-hidden": "true" }),
          el("span", { class: "what" }, WORKING[round] || "working"), t);
      }
      case "done":
        return el("span", { class: "cell done", title: c.title || null }, icon("check"),
          c.text ? el("span", {}, c.text) : null, c.ms !== null ? el("span", { class: "t" }, secs(c.ms)) : null);
      case "failed":
        return el("span", { class: "cell failed", title: c.title || null }, icon("x"), el("span", {}, c.text || "failed"));
      case "skipped":
        return el("span", { class: "cell idle" }, c.text || "–");
      case "stopped":
        return el("span", { class: "cell idle" }, "stopped");
      default:
        return this.finished
          ? el("span", { class: "cell idle" }, "–")
          : el("span", { class: "cell queued" }, el("i", { class: "ring", "aria-hidden": "true" }), el("span", { class: "what" }, "waiting"));
    }
  }

  // What Triage was asked and what came of it: text the server wrote, shown as text
  addTriageNote(d) {
    for (const round of Array.isArray(d.skipped) ? d.skipped : []) this.skipped.add(String(round));
    this.triageNode.append(el("li", { class: `triage-note${d.acted ? " acted" : ""}${d.ok ? "" : " failed"}` },
      icon("zap"), el("span", {}, String(d.note || ""))));
  }

  tick() {
    const now = performance.now();
    for (const a of this.agents.values()) {
      for (const c of a.cells) {
        if (c && c.state === "working" && c.timer) c.timer.textContent = secs(now - c.started);
      }
    }
  }

  // The verdict as it's written; the finished one replaces it. Rendered as
  // Markdown into text nodes, at most once a frame.
  appendDraft(text) {
    this.draftText += text;
    if (!this.draft) {
      this.draftBody = el("div", { class: "md draft-text streaming" });
      this.draft = el("section", { class: "draft", id: "draft", "aria-label": "Verdict, being written" },
        el("header", { class: "verdict-head" },
          el("span", { class: "verdict-title" }, icon("scale"), "Verdict"),
          el("span", { class: "writing" }, el("i", { class: "spin", "aria-hidden": "true" }), "being written…")),
        this.draftBody);
      this.slot.replaceChildren(this.draft);
      this.setStatus("Writing the verdict…", true);
    }
    if (this.draftQueued) return;
    this.draftQueued = true;
    requestAnimationFrame(() => {
      this.draftQueued = false;
      if (!this.draft) return;
      follow(this, () => this.draftBody.replaceChildren(renderMarkdown(this.draftText)));
    });
  }

  complete(r) {
    this.done = true;
    this.result = r;
    // The result's own record replaces the notes that came as events (and restores them after a reload)
    this.triageNode.replaceChildren();
    for (const d of Array.isArray(r.triage) ? r.triage : []) this.addTriageNote(d);
    for (const a of this.agents.values()) {
      for (const c of a.cells) {
        if (c && c.state === "working") Object.assign(c, { state: "done", ms: performance.now() - c.started });
      }
    }
    this.draft = null;
    this.renderResult();
  }

  fail(message) {
    this.note = message;
    this.noteKind = "error";
    this.slot.append(el("div", { class: "notice error", role: "alert" }, icon("alert"), el("span", {}, message)));
  }

  stop(message) {
    this.note = message;
    this.noteKind = "info";
    if (this.draft) this.draft.remove();
    this.draft = null;
    this.slot.append(el("div", { class: "notice" }, icon("info"), el("span", {}, message)));
  }

  // Nothing more will happen: settle the grid and the status line
  finish() {
    this.finished = true;
    for (const a of this.agents.values()) {
      for (const c of a.cells) if (c && c.state === "working") c.state = "stopped";
    }
    const r = this.result;
    this.el.classList.remove("running");
    if (r && r.final) {
      this.setStatus(`${plural(r.calls, "call")} · ${secs(r.elapsed_ms)}`);
    } else if (this.noteKind === "info") {
      this.setStatus("Stopped");
    } else {
      this.setStatus("No answer");
      this.el.classList.add("failed");
    }
    this.renderGrid();
  }

  renderResult() {
    const r = this.result;
    const out = [];
    if (r.final) out.push(verdictCard(r));
    else out.push(el("div", { class: "notice error", role: "alert" }, icon("alert"),
      el("span", {}, r.error || "The panel produced no answer.")));
    const tabs = [];
    if (r.standings.some((s) => s.verdicts.length)) tabs.push(["review", "Peer review", null, () => reviewPanel(r)]);
    if (r.answers.length) tabs.push(["answers", "Answers", r.answers.length, () => answersPanel(r)]);
    if (r.failures.length) tabs.push(["problems", "Problems", r.failures.length, () => failuresPanel(r)]);
    if (tabs.length) out.push(this.tabs(tabs));
    this.slot.replaceChildren(...out);
  }

  tabs(list) {
    const id = `t${nextId++}`;
    const buttons = [];
    const panels = [];
    const select = (key, focus) => {
      this.tab = key;
      buttons.forEach((b) => {
        const on = b.dataset.tab === key;
        b.setAttribute("aria-selected", on ? "true" : "false");
        b.tabIndex = on ? 0 : -1;
        if (on && focus) b.focus();
      });
      panels.forEach((p) => { p.hidden = p.dataset.tab !== key; });
    };
    for (const [key, title, count, render] of list) {
      const button = el("button", { type: "button", role: "tab", class: "tab", id: `${id}-${key}`,
        "aria-controls": `${id}-${key}-panel`, "data-tab": key },
        title, count !== null ? el("span", { class: "count" }, String(count)) : null);
      button.addEventListener("click", () => select(key));
      buttons.push(button);
      panels.push(el("div", { class: `tabpanel ${key}`, role: "tabpanel", id: `${id}-${key}-panel`,
        "aria-labelledby": `${id}-${key}`, "data-tab": key }, render()));
    }
    const bar = el("div", { class: "tabs", role: "tablist", "aria-label": "Details" }, buttons);
    bar.addEventListener("keydown", (e) => {
      const at = buttons.findIndex((b) => b.dataset.tab === this.tab);
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (step) {
        e.preventDefault();
        select(buttons[(at + step + buttons.length) % buttons.length].dataset.tab, true);
      }
    });
    select(list.some(([key]) => key === this.tab) ? this.tab : list[0][0]);
    return el("section", { class: "details", "aria-label": "Details" }, bar, panels);
  }

  snapshot() {
    return {
      question: this.question, mode: this.mode, auto: Boolean(this.auto), followup: this.followup, result: this.result,
      pictures: this.pictureCount, private: Boolean(this.private),
      note: this.note, noteKind: this.noteKind, rounds: this.rounds, at: this.at, tab: this.tab,
      agents: [...this.agents.values()].map((a) => ({
        name: a.name, label: a.label, model: a.model, letter: a.letter, note: a.note, failed: a.failed,
        cells: a.cells.map((c) => (c ? { state: c.state, text: c.text, title: c.title, ms: c.ms } : null)),
      })),
    };
  }

  static restore(topic, s) {
    const count = Math.min(Math.max(0, Math.floor(Number(s.pictures) || 0)), pics.MAX_PICTURES);
    const turn = new Turn(topic, String(s.question), MODE_ROUNDS[s.mode] ? s.mode : "review", Boolean(s.followup),
      Array.from({ length: count }, () => ({ url: "" })));
    if (Array.isArray(s.rounds)) turn.rounds = s.rounds.map(String);
    turn.auto = Boolean(s.auto);
    turn.private = Boolean(s.private);
    if (turn.auto) turn.modeTag.textContent = `Auto · ${MODE_TITLES[turn.mode] || turn.mode}`;
    turn.at = Number(s.at) || 0;
    turn.tab = String(s.tab || "");
    for (const a of Array.isArray(s.agents) ? s.agents : []) {
      turn.agents.set(String(a.name), {
        name: String(a.name), label: String(a.label), model: String(a.model || ""), letter: String(a.letter || ""),
        note: String(a.note || ""), failed: Boolean(a.failed),
        cells: (Array.isArray(a.cells) ? a.cells : []).map((c) => (c ? {
          state: String(c.state), text: String(c.text || ""), title: String(c.title || ""),
          ms: typeof c.ms === "number" ? c.ms : null, started: 0,
        } : undefined)),
      });
    }
    if (s.result && typeof s.result === "object") turn.complete(s.result);
    if (s.note) (s.noteKind === "error" ? turn.fail : turn.stop).call(turn, String(s.note));
    turn.finish();
    turn.setExpanded(false);
    return turn;
  }
}

// ── Result pieces ─────────────────────────────────────────────────────────

function letter(label) {
  return el("span", { class: "letter" }, label);
}

function callout(title, items, kind) {
  return el("div", { class: `callout ${kind}` }, el("h3", {}, icon(kind === "disputed" ? "alert" : "check"), title),
    el("ul", {}, items.map((t) => el("li", {}, inline(t)))));
}

function confidence(level) {
  const n = CONFIDENCE[level];
  if (!n) return null;
  return el("span", { class: `conf conf-${level}`, title: `The panel's confidence: ${level}` },
    el("span", { class: "bars", "aria-hidden": "true" }, [1, 2, 3].map((i) => el("i", { class: i <= n ? "on" : "" }))),
    `${level[0].toUpperCase()}${level.slice(1)} confidence`);
}

function verdictCard(r) {
  const f = r.final;
  const saver = r.mode === "saver";
  const role = saver && ["confirmed", "corrected"].includes(r.verifier_outcome) ? "verified by"
    : saver ? "from" : "moderated by";
  return el("section", { class: "verdict", "aria-label": "Ixel verdict" },
    el("header", { class: "verdict-head" },
      el("span", { class: "verdict-title" }, icon("scale"), "Verdict"),
      confidence(f.confidence),
      el("span", { class: "by" }, `${role} ${f.moderator_label}`),
      el("span", { class: "spacer" }),
      copyButton(() => f.answer, "Copy the verdict")),
    el("div", { class: "md" }, renderMarkdown(f.answer)),
    f.corrections.length ? callout("Corrections", f.corrections, "corrections") : null,
    f.disagreements.length ? callout("Still disputed", f.disagreements, "disputed") : null,
    f.note ? el("p", { class: "note" }, icon("info"), el("span", {}, f.note)) : null,
    el("footer", { class: "verdict-foot" },
      el("span", { class: "tag" }, MODE_TITLES[r.mode] || r.mode),
      el("span", { class: "tag" }, `${plural(r.calls, "call")} · ${secs(r.elapsed_ms)}`),
      saver ? el("span", { class: "tag cheap" },
        `${r.tier_calls.panel || 0} cheap calls · ${r.tier_calls.verifier || 0} big-model`) : null,
      r.triage_calls ? el("span", { class: "tag triage-tag" }, plural(r.triage_calls, "triage call")) : null,
      r.earlier_turns ? el("span", { class: "tag followup-badge" }, "follow-up") : null,
      r.material ? el("span", { class: "tag", title: r.material.title },
        `code: ${r.material.chars.toLocaleString()} characters`) : null,
      r.saving && r.saving.summary ? el("span", { class: "tag cheap" }, r.saving.summary) : null,
      r.usage && r.usage.summary ? el("span", { class: "tag", title: "What this review cost. Calls on a " +
        "subscription use your plan's limits, not money; token counts marked \"about\" were estimated." },
        r.usage.summary) : null));
}

function scoreBar(score) {
  if (score === null || score === undefined) return el("span", { class: "num" }, "—");
  const grade = score >= 0.75 ? "good" : score >= 0.4 ? "mid" : "bad";
  const fill = el("i");
  fill.style.width = `${Math.round(score * 100)}%`;  // CSSOM, not an inline style attribute
  return el("div", { class: `score ${grade}` }, el("div", { class: "bar" }, fill), el("b", {}, score.toFixed(2)));
}

function reviewPanel(r) {
  const byLabel = Object.fromEntries(r.answers.map((a) => [a.label, a]));
  const rows = r.standings.map((s) => el("tr", {},
    el("td", {}, letter(s.label)),
    el("td", { class: "model-col" }, s.agent_label),
    el("td", {}, scoreBar(s.score)),
    el("td", { class: "verdicts" }, s.verdicts.map((v) => el("span", {
      class: `vchip v-${v.verdict}`, title: `${v.reviewer_label}: ${v.verdict.replace("_", " ")}`,
    }, VERDICT_GLYPHS[v.verdict] || "?"))),
    el("td", { class: "num" }, s.best_votes || "—"),
    el("td", { class: `num${s.flagged_errors.length ? " bad" : ""}` }, s.flagged_errors.length || "—")));

  const issues = r.standings.flatMap((s) => s.flagged_errors.map((e) =>
    el("li", {}, letter(s.label), el("div", {},
      el("span", { class: "by" }, `${s.agent_label} · flagged by ${e.reviewer_label}`), el("p", {}, inline(e.error))))));
  const concessions = r.concessions.map((c) => {
    const best = byLabel[c.best];
    return el("li", {}, icon("reply"),
      `${c.reviewer_label} conceded: rated ${c.best} (${best ? best.agent_label : "?"}) above its own answer`);
  });
  const agreement = r.agreement === "strong"
    ? el("p", { class: "agreement strong" }, icon("check"), "Agreement: strong. Reviewers were consistent about every answer.")
    : r.agreement === "split"
      ? el("p", { class: "agreement split" }, icon("alert"),
        `Agreement: split. Reviewers contradicted each other on answer ${r.disputed.join(", ")}.`)
      : null;

  return el("div", { class: "review" },
    el("div", { class: "table-wrap" }, el("table", { class: "scores" },
      el("thead", {}, el("tr", {}, ["", "Model", "Peer score", "Verdicts", "Picked best", "Issues"].map((h) =>
        el("th", { scope: "col" }, h)))),
      el("tbody", {}, rows))),
    el("p", { class: "fine" }, "Scores come from the other models only: a model's grade of its own answer doesn't count."),
    agreement,
    concessions.length ? el("ul", { class: "concessions" }, concessions) : null,
    issues.length ? el("div", { class: "issues-wrap" }, el("h3", {}, "Flagged issues"), el("ul", { class: "issues" }, issues)) : null);
}

function answersPanel(r) {
  return el("div", { class: "answers" }, r.answers.map((a) => {
    const body = el("div", { class: "md" }, renderMarkdown(a.text));
    const long = a.text.length > 1400 || a.text.split("\n").length > 24;
    const card = el("article", { class: `answer${long ? " clamped" : ""}` },
      el("header", {}, letter(a.label), el("span", { class: "who" }, a.agent_label),
        el("span", { class: "meta" }, secs(a.latency_ms)),
        a.original_text ? el("span", { class: "pill" }, "revised") : null,
        el("span", { class: "spacer" }),
        copyButton(() => a.text, `Copy answer ${a.label}`)),
      body);
    if (long) {
      const more = el("button", { type: "button", class: "linkish more" }, "Show all");
      more.addEventListener("click", () => {
        const clamped = card.classList.toggle("clamped");
        more.textContent = clamped ? "Show all" : "Show less";
      });
      card.append(more);
    }
    if (a.original_text) {
      card.append(el("details", { class: "original" },
        el("summary", {}, "The answer before revision"), el("div", { class: "md" }, renderMarkdown(a.original_text))));
    }
    return card;
  }));
}

function failuresPanel(r) {
  return el("ul", { class: "failures" }, r.failures.map((f) =>
    el("li", {}, icon("alert"), el("div", {}, el("b", {}, f.agent_label),
      el("span", { class: "by" }, ` · ${ROUND_TITLES[f.round] || f.round}`), el("p", {}, f.error)))));
}

// ── Running a review ──────────────────────────────────────────────────────

// While another view is on screen Ask has no size, so scrolling waits for it to come back:
// { following: whether you were at the newest text, reveal: what to bring into view }
let away = null;

const nearBottom = () => (away ? away.following
  : scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160);

// Scrolls node to the top of the thread
function reveal(node) {
  if (away) { away.reveal = node; return; }
  scroller.scrollTop += node.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 16;
}

// Keeps the newest text in view while it streams, unless you've scrolled up to read
// (or you're reading another conversation)
function follow(turn, update) {
  const stick = turn.topic === active && nearBottom();
  update();
  if (stick) scroller.scrollTop = scroller.scrollHeight;
}

function setBusy(busy) {
  document.body.classList.toggle("busy", busy);
  $("#ask").hidden = busy;
  $("#ask").disabled = busy;
  $("#stop").hidden = !busy;
}

// ── /handoff: parts of one request to different agents (Handoff's dispatch) ──

const KIND_WORDS = { edit: "changes files", review: "reviews", answer: "answers", image: "makes pictures" };
const HANDOFF_EXAMPLE = "/handoff codex review my changes, grok make pictures of my app, claude finish the next " +
  "step, and gemini make me a list of projects to check out";

class HandoffCard {
  constructor(topic, request) {
    this.topic = topic;
    this.request = request;
    this.project = el("input", { type: "text", class: "handoff-project", spellcheck: "false",
      placeholder: "The project's folder, like C:\\Users\\you\\code\\shop", "aria-label": "Project folder" });
    this.planButton = el("button", { type: "button", class: "handoff-btn" }, "Plan");
    this.status = el("span", { class: "status", "aria-live": "polite" });
    this.body = el("div", { class: "handoff-body" });
    this.planButton.addEventListener("click", () => this.plan());
    this.project.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); this.plan(); } });
    // A plan belongs to the folder it was made for: a changed folder needs a new plan before Run
    this.project.addEventListener("input", () => this.planChanged());
    this.el = el("article", { class: "turn handoff-turn" },
      el("div", { class: "you" }, el("div", { class: "bubble" }, `/handoff ${request}`)),
      el("div", { class: "reply" },
        el("div", { class: "reply-head" },
          el("span", { class: "avatar" }, el("img", { src: "/mark.svg", alt: "", width: "16", height: "16" })),
          el("span", { class: "who" }, "Handoff"), this.status),
        el("div", { class: "handoff-card" },
          el("div", { class: "handoff-where" }, el("label", {}, "Project"), this.project, this.planButton),
          this.body)));
  }

  setStatus(text, working = false) {
    const label = el("span", { class: "status-text" }, text);
    this.status.replaceChildren(...(working ? [el("i", { class: "spin", "aria-hidden": "true" }), label] : [label]));
  }

  async info() {
    try { return await (await api("/api/handoff")).json(); } catch (e) { return { installed: true, project: "" }; }
  }

  // Ixel MAT can be installed without Handoff: show the one line that adds it on this system
  notInstalled(info) {
    const how = info.install || {};
    this.fail("Handoff isn't installed on this computer.");
    if (how.command) {
      this.body.append(el("p", { class: "handoff-hint" }, `To add it, run this in ${how.where}, then open Ixel again:`),
        el("p", { class: "handoff-example" }, how.command));
    }
  }

  // Just /handoff: how to use it, or how to add it
  async explain() {
    this.planButton.hidden = true;
    const info = await this.info();
    if (!info.installed) { this.notInstalled(info); return; }
    this.setStatus("");
    this.body.replaceChildren(el("p", { class: "handoff-hint" },
      "Hand parts of one request to different agents. Start each part with who does it:"),
      el("p", { class: "handoff-example" }, HANDOFF_EXAMPLE));
  }

  async start() {
    const info = await this.info();  // if this can't be asked, Plan asks again
    if (!info.installed) { this.notInstalled(info); return; }
    if (this.planning || this.planned !== undefined) return;  // they pressed Plan while this was asked
    if (!this.project.value.trim()) this.project.value = rememberedProject() || info.project || "";
    if (this.project.value) this.plan();
    else {
      this.setStatus("Which project is this for?");
      this.project.focus();
    }
  }

  fail(message) {
    this.setStatus("");
    this.body.replaceChildren(el("p", { class: "handoff-error" }, icon("alert"), el("span", {}, message)));
  }

  busy(on) {
    this.planButton.disabled = on;
    this.project.disabled = on;
  }

  async plan() {
    const project = this.project.value.trim();
    if (this.planning) return;  // one plan at a time, so the one shown is the one Run runs
    if (!project) { this.project.focus(); return; }
    this.planning = true;
    this.busy(true);
    this.setStatus("Making a plan…", true);
    this.body.replaceChildren();
    try {
      const data = await postJSON("/api/handoff/plan", { project, request: this.request });
      rememberProject(data.project || project);
      this.project.value = data.project || project;
      this.planned = this.project.value;
      this.showPlan(data);
    } catch (e) {
      this.fail(e.message);
    } finally {
      this.planning = false;
      this.busy(false);
    }
  }

  planChanged() {
    if (!this.actions || !this.actions.isConnected || this.project.value.trim() === this.planned) return;
    this.actions.remove();
    this.setStatus("The folder changed. Press Plan to see the plan for it.");
  }

  stepRow(step, n) {
    const head = el("div", { class: "handoff-step-head" },
      el("span", { class: "n" }, `${n}.`), el("b", {}, step.label || step.agent),
      el("span", { class: "kind" }, KIND_WORDS[step.kind] || step.kind),
      step.detail ? el("span", { class: "detail" }, `· ${step.detail}`) : null);
    const row = el("li", { class: step.problem ? "blocked" : "" }, head,
      el("div", { class: "handoff-title" }, step.problem ? step.text : step.title));
    if (step.note) row.append(el("div", { class: "handoff-note" }, step.note));
    if (step.problem) row.append(el("div", { class: "handoff-problem" }, `Won't run: ${step.problem}`));
    return row;
  }

  showPlan(data) {
    const steps = data.steps || [];
    if (!steps.length) {
      this.setStatus("");
      this.body.replaceChildren(el("p", { class: "handoff-hint" },
        "Start each part with who does it, for example:"), el("p", { class: "handoff-example" }, HANDOFF_EXAMPLE));
      return;
    }
    const runnable = steps.filter((s) => !s.problem);
    this.list = el("ol", { class: "handoff-steps" }, ...steps.map((s, i) => this.stepRow(s, i + 1)));
    const nodes = [this.list];
    for (const problem of data.problems || []) nodes.push(el("p", { class: "handoff-hint" }, problem));
    if (runnable.length) {
      const edits = runnable.some((s) => s.kind === "edit");
      nodes.push(el("p", { class: "handoff-hint" }, (edits
        ? "Edits happen in their own branch (handoff/T-N); nothing is pushed. " : "") +
        "Answers, reviews and pictures change nothing in the project. Each task comes back to you on the board."));
      const run = el("button", { type: "button", class: "ask handoff-run" },
        `Run ${runnable.length} task${runnable.length === 1 ? "" : "s"}`);
      const cancel = el("button", { type: "button", class: "linkish" }, "Cancel");
      run.addEventListener("click", () => this.run(run, cancel, runnable.length));
      cancel.addEventListener("click", () => {
        run.remove(); cancel.remove();
        this.setStatus("Nothing was added.");
      });
      this.actions = el("div", { class: "handoff-actions" }, run, cancel);
      nodes.push(this.actions);
      this.setStatus(`${runnable.length} of ${steps.length} part${steps.length === 1 ? "" : "s"} can run`);
    } else {
      this.setStatus("None of these can run here");
    }
    this.body.replaceChildren(...nodes);
  }

  async run(runButton, cancel, count) {
    runButton.disabled = true;
    cancel.remove();
    this.busy(true);
    this.planButton.hidden = true;
    this.setStatus(`Running ${count} task${count === 1 ? "" : "s"} side by side…`, true);
    const project = this.planned;
    try {
      const data = await postJSON("/api/handoff/run", { project, request: this.request });
      this.actions.remove();
      const results = data.results || [];
      const ok = results.filter((r) => r.ok).length;
      this.setStatus(`${ok} of ${results.length} done`);
      const list = el("ul", { class: "handoff-results" }, ...results.map((r) =>
        el("li", { class: r.ok ? "ok" : "bad" }, icon(r.ok ? "check" : "x"),
          el("span", {}, el("b", {}, `${r.task} ${r.agent}`),
            `: ${r.message === "handed to human" ? "done, back to you" : r.message}`,
            r.branch ? ` · the work is on ${r.branch}` : ""))));
      const open = el("button", { type: "button", class: "handoff-btn" }, "Open the Board");
      open.addEventListener("click", () => { location.hash = "#/board"; });
      this.body.append(list, el("div", { class: "handoff-actions" }, open,
        el("span", { class: "handoff-hint" }, "Each result is on the board, with its answers and pictures.")));
      if (this.topic === active) reveal(list);
    } catch (e) {
      if (this.actions) this.actions.remove();
      this.body.append(el("p", { class: "handoff-error" }, icon("alert"), el("span", {}, e.message)));
      this.setStatus("");
    }
  }
}

function startHandoff(request) {
  const topic = active || newTopic(`/handoff ${request}`.trim());
  const card = new HandoffCard(topic, request);
  topic.thread.append(card.el);
  show(topic);
  reveal(card.el);
  if (request) card.start();
  else card.explain();
}

let starting = false;  // waiting for pictures to finish attaching before asking

async function ask() {
  if (starting) return;
  const box = $("#question");
  const codeBox = $("#material");
  if (/^\/handoff\b/i.test(box.value.trim()) && !current) {
    startHandoff(box.value.trim().replace(/^\/handoff\b/i, "").trim());
    box.value = "";
    grow();
    return;
  }
  if (current || $("#ask").disabled) return;
  if (voice.state !== "idle") {  // its words belong in the question first
    showNotice(voice.state === "recording" ? "Stop the recording first, so its words are in your question."
      : "The sound is still being written out. Ask once its words are in your question.", "info");
    voice.warned = true;
    return;
  }
  if (attached.some((p) => !p.id)) {  // still being made smaller and sent to Ixel (well under a second each)
    const before = failures;
    starting = true;
    $("#ask").classList.add("waiting");
    try {
      while (attached.some((p) => !p.id)) await trayChanged();  // attached, failed or taken out
    } finally {
      starting = false;
      $("#ask").classList.remove("waiting");
    }
    if (current || failures !== before) return;  // one couldn't be attached: its notice says why
  }
  const code = codeBox.value;
  const typed = box.value.trim();
  const question = typed || (code.trim() ? "Review the attached code." : "") ||
    (attached.length ? `What do you make of the attached ${attached.length === 1 ? "picture" : "pictures"}?` : "");
  if (!question) return;
  if (question.length > MAX_QUESTION_CHARS) {
    showNotice(TOO_LONG(question.length), "error");
    return;
  }
  if (code.length > MAX_CODE_CHARS) {  // said here, before a large paste is sent at all
    showNotice(TOO_MUCH_CODE(code.length), "error");
    return;
  }
  if (attached.length > pics.MAX_PICTURES) {
    showNotice(`A question can have at most ${pics.MAX_PICTURES} pictures. Take some out first.`, "error");
    return;
  }
  const sent = attached.splice(0);
  for (const p of sent) p.sent = true;  // its question shows it from now on
  hideNotice();
  const earlier = earlierFor(active);
  const topic = active && !keptPrivate(active) ? active : newTopic(question);
  for (const t of topic.turns) t.setExpanded(false);
  const turn = new Turn(topic, question, mode, earlier.length > 0, sent);
  turn.private = privateOn();
  topic.turns.push(turn);
  topic.thread.append(turn.el);
  current = { turn, controller: new AbortController() };
  show(topic);
  box.value = "";
  codeBox.value = "";
  renderTray();
  grow();
  setBusy(true);
  const ticker = setInterval(() => turn.tick(), 100);
  let picturesGone = false;  // Ixel no longer had them (kept 30 minutes): they're sent to it again
  try {
    const res = await api("/api/review", {
      method: "POST", signal: current.controller.signal, body: JSON.stringify({
        question: typed, mode, material: code, pictures: sent.map((p) => p.id),
        earlier: earlier.map((t) => ({
          question: t.question.slice(0, EARLIER_CHARS), answer: t.answer.slice(0, EARLIER_CHARS),
          ...(t.private ? { private: true } : {}),  // so a server whose Private went off holds it back
        })),
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      if (res.status === 413) throw new Error(TOO_MUCH_CODE(code.length));  // over the server's size cap
      if (err.code === "pictures_gone") picturesGone = true;
      throw new Error(err.error || `Request failed (${res.status})`);
    }
    await readEvents(res, (event) => handleEvent(turn, event));
    if (!turn.done && !turn.note) turn.fail("The connection closed before the panel finished.");
  } catch (e) {
    if (e.name === "AbortError") turn.stop("Stopped. Model calls that were still running were cancelled.");
    else turn.fail(e.message);
  } finally {
    clearInterval(ticker);
    current = null;
    turn.finish();
    setBusy(false);
    if (!turn.done) {  // so it can be asked again (beside anything added meanwhile)
      if (!box.value.trim()) box.value = typed;
      if (!codeBox.value.trim()) codeBox.value = code;
      attached.unshift(...sent.filter((p) => !attached.includes(p)));
      if (picturesGone) {
        for (const p of sent) {
          p.id = "";
          attach(p);
        }
      }
      renderTray();
      grow();
    }
    renderFollowup();
    renderConvos();
    save();
    if (panelStale) {
      panelStale = false;
      settingsChanged();
    }
  }
}

async function readEvents(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer));
}

function handleEvent(turn, { kind, data }) {
  if (kind === "saves") {
    showSaves({ ...data, next_milestone: null }, data.saved);
    loadSaves();  // the full numbers, with the next milestone
    if (data.saved) toast("Saved one", "Your cheaper models handled that; the big model didn't have to.");
    for (const a of data.unlocked) toast(`★ ${a.title}`, a.description);
    return;
  }
  if (kind !== "final") {
    follow(turn, () => turn.handle(kind, data));
    return;
  }
  // Done: if you were following along, show the verdict from its first line
  const following = turn.topic === active && nearBottom();
  turn.handle(kind, data);
  const verdict = $(".verdict", turn.el);
  if (following && verdict && (away || verdict.getBoundingClientRect().top < scroller.getBoundingClientRect().top)) {
    reveal(verdict);
  }
  renderFollowup();
}

// ── Composer ──────────────────────────────────────────────────────────────

function grow() {
  const box = $("#question");
  box.style.height = "auto";  // CSSOM, not an inline style attribute
  box.style.height = `${Math.min(box.scrollHeight, Math.round(window.innerHeight * 0.4))}px`;
  const code = $("#material").value.trim();
  $("#ask").classList.toggle("blank", !box.value.trim() && !code && !attached.length);
  $("#attach-toggle").classList.toggle("has-code", Boolean(code));
}

// ── Pictures attached to the next question ────────────────────────────────

// Each: { blob, url, width, height, id (Ixel's, once it has it), sent (a question shows it) }
const attached = [];
let failures = 0;          // pictures that couldn't be attached (a question waiting on them isn't sent)
let waiters = [];          // ask() waiting for the tray to change

function trayChanged() {
  return new Promise((resolve) => waiters.push(resolve));
}

// Made smaller here, then sent to Ixel; a picture taken out meanwhile stops there
function attach(item) {
  (async () => {
    try {
      if (!item.blob) {
        const { blob, width, height } = await pics.prepare(item.file);
        if (!attached.includes(item)) return;
        Object.assign(item, { blob, url: URL.createObjectURL(blob), width, height, file: null });
        renderTray();
      }
      const id = await pics.upload(item.blob);
      if (attached.includes(item)) item.id = id;
    } catch (e) {
      if (!attached.includes(item)) return;
      failures += 1;
      dropPicture(item);
      showNotice(e.message, "error");
    }
    renderTray();
  })();
}

// → how many went in the tray
function addPictures(files) {
  let added = 0;
  for (const file of files) {
    if (!file.type.startsWith("image/")) {
      showNotice(`${file.name || "That file"} isn't a picture.`, "error");
      continue;
    }
    if (attached.length >= pics.MAX_PICTURES) {
      showNotice(`A question can have at most ${pics.MAX_PICTURES} pictures.`, "error");
      break;
    }
    const item = { file, blob: null, url: "", width: 0, height: 0, id: "" };
    attached.push(item);
    attach(item);
    added += 1;
  }
  renderTray();
  grow();
  return added;
}

function dropPicture(item) {
  const at = attached.indexOf(item);
  if (at >= 0) attached.splice(at, 1);
  if (item.url && !item.sent) URL.revokeObjectURL(item.url);
  renderTray();
  grow();
}

// Which models will see them: said before asking, not after
function trayNote() {
  const seeing = panel.agents.filter((a) => a.pictures);
  if (!seeing.length) {
    return { kind: "warn", text: "None of your models can see pictures, so they'll only be told there is one. " +
      "Turn one on in Settings, under Pictures." };
  }
  if (seeing.length === panel.agents.length) return { kind: "", text: "Every model on the panel sees them." };
  const names = seeing.map((a) => a.label);
  const list = names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
  return { kind: "", text: `Only ${list} ${names.length === 1 ? "sees" : "see"} them; the others are told there's a picture they can't see.` };
}

function renderTray() {
  const tray = $("#pic-tray");
  tray.hidden = !attached.length;
  $("#picture-add").classList.toggle("has-code", attached.length > 0);
  const focused = attached.find((p) => p.button && p.button === document.activeElement);
  $("#pic-list").replaceChildren(...attached.map((p, i) => {
    p.button = el("button", {
      type: "button", class: "icon-btn pic-remove", "aria-label": `Remove picture ${i + 1}`, title: "Remove",
      onclick: () => { dropPicture(p); $("#question").focus(); },
    }, icon("x"));
    return el("li", { class: `pic ${p.id ? "" : "working"}` },
      p.url ? el("img", { src: p.url, alt: `Picture ${i + 1}`, width: String(p.width), height: String(p.height) })
        : el("span", { class: "pic-blank", "aria-hidden": "true" }),
      p.id ? null : el("i", { class: "spin", role: "img", "aria-label": "attaching" }),
      p.button);
  }));
  if (focused) focused.button.focus();  // a redraw while you're on a picture leaves you there
  if (attached.length) {
    const note = trayNote();
    const line = $("#pic-note");
    line.className = `pic-note ${note.kind}`;
    if (line.textContent !== note.text) line.textContent = note.text;
  }
  const waiting = waiters;
  waiters = [];
  for (const resolve of waiting) resolve();
}

// ── Sound written out into the question ───────────────────────────────────

// idle | recording | writing. `rec` is the recording, `writing` the request's controller; `turn` changes
// with every start and cancel, so a microphone or a recorder that answers late is let go. `last` is sound
// that couldn't be written out, kept to send again (and, for a recording, to save).
const voice = {
  state: "idle", rec: null, writing: null, timer: 0, said: "", turn: 0, last: null, warned: false,
  doing: "",  // what's under way, when it isn't the service writing it out (taking a video apart)
};
const soundService = () => (panel.sound && panel.sound.service) || "";
const isSound = (file) => file.type.startsWith("audio/") ||
  /\.(m4a|mp3|wav|webm|ogg|oga|opus|flac)$/i.test(file.name || "");
const inSoundBar = () => document.activeElement === document.body || $("#sound-bar").contains(document.activeElement);

function openSound(open = true) {
  $("#sound-bar").hidden = !open;
  $("#sound-add").setAttribute("aria-expanded", String(open));
  renderSound();
}

function stopVoice(said) {
  voice.turn += 1;
  if (voice.rec) voice.rec.cancel();
  if (voice.writing) voice.writing.abort();
  voice.said = said;
  setVoice("idle");
}

function closeSound() {
  stopVoice("");
  voice.last = null;
  openSound(false);
  $("#question").focus();
}

function setVoice(state) {
  voice.state = state;
  if (state !== "recording") {
    clearInterval(voice.timer);
    voice.timer = 0;
    voice.rec = null;
  }
  if (state !== "writing") {
    voice.writing = null;
    voice.doing = "";
  }
  if (state === "idle" && voice.warned) {  // Ask's "wait for the sound" is over
    voice.warned = false;
    hideNotice();
  }
  renderSound();
}

function renderSound() {
  const service = soundService();
  const { state, last } = voice;
  const busy = state !== "idle";
  $("#sound-add").classList.toggle("has-code", busy);
  $("#rec-dot").hidden = state !== "recording";
  $("#sound-rec").hidden = state !== "recording";
  $("#sound-clock").hidden = state !== "recording";
  $("#sound-record").hidden = busy || !sound.canRecord();
  $("#sound-pick").hidden = busy;
  $("#sound-record").disabled = !service;
  $("#sound-pick").disabled = !service;
  $("#sound-stop").hidden = state !== "recording";
  $("#sound-stop").disabled = !voice.rec;  // until the microphone is on
  $("#sound-cancel").hidden = !busy;
  $("#sound-retry").hidden = busy || !last || !service;
  $("#sound-save").hidden = busy || !last || !last.recorded;
  let words;
  if (!service) {  // what happened last, if anything, then what's needed
    words = [voice.said ? `${voice.said} ` : "",
      (panel.sound && panel.sound.problem) || "Writing out sound needs an OpenAI or Groq key.", " ",
      el("button", { type: "button", class: "linkish", onclick: openSettings }, "Open Settings")];
  } else if (state === "recording") {
    words = [`Recording. Stop when you're done, and ${service} writes it out into your question.`];
  } else if (state === "writing") {
    words = [el("i", { class: "spin", "aria-hidden": "true" }), voice.doing || `${service} is writing it out…`];
  } else {
    words = [voice.said || `Record, or choose a sound file. It goes to ${service} to be written out into your ` +
      "question, which you read before you ask. No model hears the sound itself."];
  }
  const line = $("#sound-state");
  const text = words.map((w) => (typeof w === "string" ? w : w.textContent)).join("");
  if (line.dataset.text !== text) {  // the same words aren't announced again
    line.dataset.text = text;
    line.replaceChildren(...words);
  }
}

function openSettings() {
  location.hash = "#/settings";
  setTimeout(() => { const title = $("#view-settings .page-title"); if (title) title.focus(); });  // after it's shown
}

async function startRecording() {
  if (voice.state !== "idle" || !soundService()) return;
  const turn = ++voice.turn;
  voice.said = "";
  voice.last = null;
  $("#sound-clock").textContent = sound.clock(0);
  setVoice("recording");  // a second press while the microphone is asked for does nothing
  $("#sound-cancel").focus();
  let rec;
  try {
    rec = await sound.record(() => {
      if (voice.rec === rec) stopRecording("The microphone stopped, so the recording ended there.");
    });
  } catch (e) {
    if (turn === voice.turn) {
      stopVoice(e.message);
      if (inSoundBar()) $("#sound-pick").focus();
    }
    return;
  }
  if (turn !== voice.turn) {  // cancelled while the microphone was asked for
    rec.cancel();
    return;
  }
  voice.rec = rec;
  renderSound();
  const tick = () => {
    const seconds = (performance.now() - rec.started) / 1000;
    $("#sound-clock").textContent = sound.clock(seconds);
    if (seconds >= sound.MAX_RECORDING) {
      stopRecording(`The recording stopped at ${sound.MAX_RECORDING / 60} minutes, the most one can be.`);
    }
  };
  voice.timer = setInterval(tick, 500);
  if (inSoundBar()) $("#sound-stop").focus();
}

// Stop pressed, or `why` it stopped by itself (then where you are typing is left alone)
async function stopRecording(why = "") {
  if (voice.state !== "recording" || !voice.rec) return;
  const { rec } = voice;
  const moveFocus = !why && inSoundBar();
  const controller = new AbortController();
  setVoice("writing");  // shown at once: the last of the sound comes in as the recorder stops
  voice.writing = controller;
  if (moveFocus) $("#sound-cancel").focus();
  const blob = await rec.stop();
  if (voice.writing === controller) send({ blob, recorded: true }, controller, why);
}

// A sound file picked or dropped → whether it's being written out
function writeOut(file) {
  openSound();
  if (!soundService()) return false;  // the bar says what's needed
  if (voice.state !== "idle") {
    showNotice("Wait for the sound under way to be written out first.", "error");
    return false;
  }
  start({ blob: file, recorded: false, video: video.isVideo(file) ? { seconds: null } : null });
  return true;
}

// A video picked or dropped: frames from it go in the tray, and its sound is written out into the question
// → whether it's under way
function addVideo(file) {
  openSound();
  if (voice.state !== "idle") {
    showNotice("Wait for the sound under way to be written out first.", "error");
    return false;
  }
  const controller = new AbortController();
  voice.turn += 1;
  voice.said = "";
  voice.last = null;
  const moveFocus = inSoundBar();
  setVoice("writing");
  voice.writing = controller;
  voice.doing = `Taking frames from ${file.name || "the video"}…`;
  renderSound();
  if (moveFocus) $("#sound-cancel").focus();
  (async () => {
    const full = `No frames were taken: a question can have at most ${pics.MAX_PICTURES} pictures.`;
    let found = { files: [], seconds: null };
    let note = full;
    let unread = false;
    if (await video.soundOnly(file).catch(() => false)) {  // a sound file after all
      if (voice.writing !== controller) return;
      if (!soundService()) {
        voice.said = "";
        setVoice("idle");
        return;
      }
      send({ blob: file, recorded: false, video: null }, controller);
      return;
    }
    const room = pics.MAX_PICTURES - attached.length;
    if (room > 0) {
      try {
        found = await video.frames(file, Math.min(video.MAX_FRAMES, room));
        note = "No frames were taken: it has no picture this browser can show.";
      } catch (e) {
        note = e.message;  // its sound may still be readable: that's tried next
        unread = true;
      }
      if (voice.writing !== controller) return;
      const added = addPictures(found.files);  // pictures put in meanwhile leave less room
      if (added) note = `${plural(added, "frame")} from it ${added === 1 ? "is" : "are"} attached.`;
      else if (found.files.length) note = full;
    }
    if (!soundService()) {
      voice.said = `${note} Its sound wasn't written out.`;  // the bar goes on to say what that needs
      setVoice("idle");
      return;
    }
    send({ blob: file, recorded: false, video: { seconds: found.seconds, unread } }, controller, note);
  })();
  return true;
}

function start(item) {
  const controller = new AbortController();
  voice.turn += 1;
  voice.said = "";
  voice.last = null;
  const moveFocus = inSoundBar();
  setVoice("writing");
  voice.writing = controller;
  if (moveFocus) $("#sound-cancel").focus();
  send(item, controller);
}

// A video's sound, taken out here → { pieces, said } (no pieces: nothing to write out, and `said` is why)
async function videoSound(item, controller) {
  const name = item.blob.name || "the video";
  voice.doing = `Taking the sound out of ${name}…`;
  renderSound();
  if (await video.soundOnly(item.blob).catch(() => false)) return { pieces: [item.blob], label: "" };  // as it is
  let { seconds } = item.video;
  if (!seconds) seconds = await video.length(item.blob).catch(() => 0);  // 0: not known
  const track = await video.soundOf(item.blob, seconds);
  if (voice.writing !== controller) return { pieces: [] };
  if (!track) {
    return { pieces: [], said: item.video.unread ? "Its sound couldn't be taken out either." : `${name} has no sound to write out.` };
  }
  return { pieces: track.pieces, cut: track.cut, label: `What's said in ${name}: ` };
}

// To be written out by the service the bar named; the words go where the cursor is in the question. A long
// video's pieces each go in turn: Send it again carries on from the one that failed.
async function send(item, controller, note = "") {
  let text = "";
  let said;
  let failed = false;
  let found = { pieces: [item.blob], label: "" };
  if (item.video) {
    try {
      found = item.found || await videoSound(item, controller);
    } catch (e) {
      found = { pieces: [], said: e.message };  // too long or too big: sending it again won't help
    }
  }
  try {
    const { pieces, cut, label, said: why } = found;
    if (!pieces.length) {
      said = why || "";
    } else {
      controller.sent = true;
      item.found = found;
      item.words = item.words || [];
      let service = "";
      for (let i = item.words.length; i < pieces.length; i += 1) {
        if (voice.writing !== controller) return;
        voice.doing = pieces.length > 1 ? `${soundService()} is writing it out, part ${i + 1} of ${pieces.length}…` : "";
        renderSound();
        try {
          const reply = await sound.transcribe(pieces[i], controller.signal, (panel.sound || {}).name);
          item.words.push(reply.text);
          service = reply.service;
        } catch (e) {
          if (e.code !== "no_words" || pieces.length === 1) throw e;
          item.words.push("");  // nothing said in this part: the others still count
        }
      }
      const words = item.words.filter(Boolean);
      if (!words.length) {
        said = "No words were heard in it.";
      } else {
        text = label + words.join(" ");
        said = `${service || soundService()} wrote it out into your question. Read it over before you ask.` +
          (cut ? ` Only its first ${video.MAX_SOUND_SECONDS / 60} minutes were written out.` : "");
      }
    }
  } catch (e) {
    said = e.message;
    failed = e.code !== "no_words";  // sending it again won't find words
    if (e.code === "sound_service_changed") settingsChanged();  // the bar names the new one
  }
  if (voice.writing !== controller) return;  // cancelled or closed meanwhile
  const focusHere = inSoundBar();
  if (text) putInQuestion(text, focusHere);
  voice.said = note ? `${note} ${said}` : said;
  voice.last = failed ? item : null;
  setVoice("idle");
  if (text && $("#question").value.length > MAX_QUESTION_CHARS) {
    showNotice(TOO_LONG($("#question").value.length), "error");
  }
  if (focusHere && !text) {
    const next = [$("#sound-retry"), $("#sound-record"), $("#sound-pick")].find((b) => !b.hidden && !b.disabled);
    (next || $("#sound-close")).focus();
  }
}

// Put in after the cursor (never over text that's selected), as typing would be, so Undo takes it out
function putInQuestion(text, focus) {
  const box = $("#question");
  const at = box.selectionEnd ?? box.value.length;
  const before = box.value.slice(0, at);
  const after = box.value.slice(at);
  const gap = !before || /\s$/.test(before) ? "" : text.length > 200 ? "\n\n" : " ";
  const words = gap + text + (after && !/^\s/.test(after) ? " " : "");
  if (focus) {
    box.focus();
    box.setSelectionRange(at, at);
    if (document.execCommand("insertText", false, words)) return;  // grow() runs on its input event
  }
  box.setRangeText(words, at, at, "end");
  grow();
}

function cancelSound() {
  const sent = Boolean(voice.writing && voice.writing.sent);
  stopVoice(sent ? "Cancelled. It may already have gone to be written out, but its words won't go into your " +
    "question." : "Cancelled. Nothing was sent.");
  ($("#sound-record").hidden ? $("#sound-pick") : $("#sound-record")).focus();
}

// Files dropped or picked: pictures to the tray, sound to be written out, a video taken apart for both
function addFiles(files) {
  const films = files.filter(video.isVideo);
  const heard = files.filter((f) => !video.isVideo(f) && isSound(f));
  const rest = files.filter((f) => !video.isVideo(f) && !isSound(f));
  if (rest.length) addPictures(rest);  // first: a video's frames go after them
  const [first] = [...films, ...heard];
  if (!first) return;
  if (!(films.includes(first) ? addVideo(first) : writeOut(first))) return;
  if (films.length + heard.length > 1) {
    showNotice("Sound and video are written out one at a time. The first one is under way.", "info");
  }
}

const hasFiles = (e) => Boolean(e.dataTransfer) && [...e.dataTransfer.types].includes("Files");

function toggleAttach() {
  const open = $("#attach").hidden;
  $("#attach").hidden = !open;
  $("#attach-toggle").setAttribute("aria-expanded", String(open));
  (open ? $("#material") : $("#question")).focus();
}

// ── Narrow screens: the sidebar is a drawer ───────────────────────────────

function openDrawer() {
  document.body.classList.add("drawer-open");
  $("#scrim").hidden = false;
  $("#close-sidebar").focus();
}

function closeDrawer() {
  if (!document.body.classList.contains("drawer-open")) return;
  document.body.classList.remove("drawer-open");
  $("#scrim").hidden = true;
}

// ── Another view on screen, and back ──────────────────────────────────────

export function hidden() {
  closeDrawer();
  if (!away) away = { following: nearBottom(), reveal: null };
}

export function shown() {
  if (!away) return;
  const { following, reveal: node } = away;
  away = null;
  grow();  // measured while hidden, the box would have no height
  if (node && node.isConnected) reveal(node);
  else if (following) scroller.scrollTop = scroller.scrollHeight;
}

// ── Wiring ────────────────────────────────────────────────────────────────

const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
fillIcons();
$("#shortcut").textContent = isMac ? "⌘↵" : "Ctrl ↵";
$("#key-mod").textContent = isMac ? "⌘" : "Ctrl";
$("#ask").addEventListener("click", ask);
$("#stop").addEventListener("click", () => current && current.controller.abort());
$("#new-question").addEventListener("click", startOver);
$("#followup-new").addEventListener("click", startOver);
$("#open-sidebar").addEventListener("click", openDrawer);
$("#close-sidebar").addEventListener("click", closeDrawer);
$("#scrim").addEventListener("click", closeDrawer);
$("#attach-toggle").addEventListener("click", toggleAttach);
$("#picture-add").addEventListener("click", () => $("#picture-file").click());
$("#picture-file").addEventListener("change", (e) => {
  addFiles([...e.target.files]);
  e.target.value = "";  // the same file can be picked again
});
$("#sound-add").addEventListener("click", () => {
  if ($("#sound-bar").hidden) {
    openSound();
    const first = [$("#sound-record"), $("#sound-pick")].find((b) => !b.hidden && !b.disabled);
    (first || $("#sound-close")).focus();
  } else {
    closeSound();
  }
});
$("#sound-close").addEventListener("click", closeSound);
$("#sound-record").addEventListener("click", startRecording);
$("#sound-stop").addEventListener("click", () => stopRecording());
$("#sound-cancel").addEventListener("click", cancelSound);
$("#sound-retry").addEventListener("click", () => { if (voice.last && voice.state === "idle") start(voice.last); });
$("#sound-save").addEventListener("click", () => { if (voice.last) sound.save(voice.last.blob); });
$("#sound-pick").addEventListener("click", () => $("#sound-file").click());
$("#sound-file").addEventListener("change", (e) => {
  const [file] = e.target.files;
  e.target.value = "";
  if (file) writeOut(file);
});
$("#question").addEventListener("paste", (e) => {
  const files = [...((e.clipboardData && e.clipboardData.files) || [])].filter((f) => f.type.startsWith("image/"));
  // Text copied from Office or a web page often comes with a picture of itself: then the text is what's meant
  if (!files.length || e.clipboardData.getData("text/plain")) return;
  e.preventDefault();
  addPictures(files);
});
// A file dropped anywhere on Ask is attached; anywhere else it's ignored (never opened in place of the app)
document.addEventListener("dragover", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  const here = !$("#view-ask").hidden;
  e.dataTransfer.dropEffect = here ? "copy" : "none";
  $(".composer").classList.toggle("dropping", here);
});
document.addEventListener("dragleave", (e) => {
  if (!e.relatedTarget) $(".composer").classList.remove("dropping");
});
document.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  $(".composer").classList.remove("dropping");
  if (!$("#view-ask").hidden) addFiles([...e.dataTransfer.files]);
});
for (const box of [$("#question"), $("#material")]) {
  box.addEventListener("input", grow);
  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); ask(); }
  });
}
document.addEventListener("keydown", (e) => {
  const typing = e.target instanceof HTMLElement && e.target.closest("input, textarea, select, [contenteditable]");
  if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey && !e.altKey && !$("#view-ask").hidden) {
    e.preventDefault();
    $("#question").focus();
  } else if (e.key === "Escape" && document.body.classList.contains("drawer-open")) {
    closeDrawer();
  }
});
const modeButtons = $$(".modes button");
for (const button of modeButtons) {
  button.addEventListener("click", () => { mode = button.dataset.mode; modePicked = true; renderModes(); });
  button.addEventListener("keydown", (e) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    const offered = modeButtons.filter((b) => !b.hidden);  // Auto only when Triage is set up
    const next = offered[(offered.indexOf(button) + step + offered.length) % offered.length];
    mode = next.dataset.mode;
    modePicked = true;
    renderModes();
    next.focus();
  });
}
restore();
show(topics.length ? topics[topics.length - 1] : null);
renderModes();
grow();
loadPanel();
