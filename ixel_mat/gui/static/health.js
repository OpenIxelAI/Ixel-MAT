// Health: whether each part of Ixel is ready, and the one line that fixes what isn't (the same
// checks as `ixel doctor`). Opening the page reads settings and looks for programs; "Check now"
// also asks each model and program whether it answers.

import { $, el, icon, copyButton, getJSON, plural } from "./common.js";

const STATE_WORDS = { ok: "Ready", warn: "Look at this", fail: "Needs fixing", off: "Not set up", unchecked: "Not checked" };
const STATE_ICONS = { ok: "check", warn: "alert", fail: "x", off: "circle", unchecked: "circle" };

let dot = null;
let report = null;
let loading = null;   // the request under way, so a second one waits for it instead of starting
let failed = "";

export function start(options) {
  dot = options.dot;
  $("#health-check").addEventListener("click", () => load(true));
  load(false);  // for the rail's dot: nothing goes over the network
}

export function shown() {
  if (!loading && !report) load(false);
  render();
}

async function load(probe) {
  if (loading) {
    if (!probe || loading.probe) return loading.promise;
    await loading.promise.catch(() => {});
  }
  const button = $("#health-check");
  button.disabled = true;
  button.classList.toggle("busy", probe);
  const promise = getJSON(`/api/health?probe=${probe ? 1 : 0}`)
    .then((data) => { report = data; failed = ""; })
    .catch((e) => { failed = e.message; })
    .finally(() => {
      loading = null;
      button.disabled = false;
      button.classList.remove("busy");
      render();
    });
  loading = { probe, promise };
  render();
  return promise;
}

const allChecks = () => (report ? report.groups.flatMap((g) => g.checks) : []);

function render() {
  const checks = allChecks();
  const count = (state) => checks.filter((c) => c.state === state).length;
  const fail = count("fail");
  const warn = count("warn");
  if (dot) {
    dot.hidden = !(fail || warn);
    dot.className = `rail-dot ${fail ? "fail" : "warn"}`;
    dot.title = fail ? `${plural(fail, "thing")} to fix` : warn ? `${plural(warn, "thing")} to look at` : "";
  }
  if ($("#view-health").hidden) return;

  const checking = loading && loading.probe;
  $("#health-check").replaceChildren(checking ? el("span", { class: "spin" }) : icon("refresh"),
    el("span", {}, checking ? "Checking…" : "Check now"));
  $("#health-when").textContent = report
    ? `${report.probed ? "Checked" : "Looked"} at ${new Date(report.checked_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
    : "";

  const summary = $("#health-summary");
  if (failed) {
    summary.className = "health-summary fail";
    summary.replaceChildren(icon("alert"), el("span", {}, `Couldn't check: ${failed}`));
  } else if (!report) {
    summary.className = "health-summary";
    summary.replaceChildren(el("span", { class: "spin" }), el("span", {}, "Looking…"));
  } else {
    const state = fail ? "fail" : warn ? "warn" : "ok";
    const parts = [];
    if (fail) parts.push(`${plural(fail, "thing")} to fix`);
    if (warn) parts.push(`${plural(warn, "thing")} to look at`);
    const unchecked = count("unchecked");
    summary.className = `health-summary ${state}`;
    summary.replaceChildren(icon(STATE_ICONS[state]), el("span", {},
      el("b", {}, parts.length ? `${parts.join(", ")}.` : unchecked && !report.probed ? "Nothing to fix so far."
        : "Everything's ready."),
      unchecked && !report.probed
        ? ` ${plural(unchecked, "check")} need${unchecked === 1 ? "s" : ""} Check now, which asks each model and program whether it answers.`
        : ""));
  }

  $("#health-groups").replaceChildren(...(report ? report.groups.map(group) : []));
}

function group(g) {
  return el("section", { class: "health-group", "aria-label": g.title },
    el("h2", {}, g.title),
    el("ul", { class: "checks" }, g.checks.map(row)));
}

function row(c) {
  return el("li", { class: `check ${c.state}` },
    el("span", { class: "check-icon", title: STATE_WORDS[c.state] }, icon(STATE_ICONS[c.state])),
    el("div", { class: "check-main" },
      el("div", { class: "check-head" },
        el("span", { class: "check-label" }, c.label),
        el("span", { class: "check-state" }, STATE_WORDS[c.state])),
      c.detail ? el("div", { class: "check-detail" }, c.detail) : null,
      c.fix ? el("div", { class: "check-fix" }, el("code", {}, c.fix), copyButton(() => c.fix, "Copy")) : null));
}
