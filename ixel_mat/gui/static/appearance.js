// Settings > Appearance: System, Light or Dark, kept for every Ixel window (see appearance.py). Picking
// one sets <html data-appearance> and theme.js switches the colors at once; then it's saved. A window
// that comes back to the front takes a choice made in another one meanwhile.

import { $, el, getJSON, postJSON } from "./common.js";

const CHOICES = [
  ["system", "System"],
  ["light", "Light"],
  ["dark", "Dark"],
];
const root = document.documentElement;
let saving = Promise.resolve();
let picks = 0;  // counts picks, so a look at the saved choice that started before one never undoes it

const current = () => root.getAttribute("data-appearance") || "system";

export function start() {
  $("#settings-appearance").replaceChildren(section());
  window.addEventListener("focus", refresh);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
}

function section() {
  const choices = CHOICES.map(([value, name]) => {
    const input = el("input", { type: "radio", name: "appearance", value, class: "sr-only", checked: value === current() });
    input.addEventListener("change", () => { if (input.checked) choose(value); });
    return el("label", { class: "look" },
      input,
      el("span", { class: `look-preview look-${value}`, "aria-hidden": "true" }, sketch("light"), sketch("dark")),
      el("span", { class: "look-name" }, name));
  });
  return el("section", { class: "set-group", "aria-labelledby": "set-h-appearance" },
    el("div", { class: "set-head" },
      el("h2", { id: "set-h-appearance" }, "Appearance"),
      el("span", { class: "set-note fail", id: "appearance-note", role: "status" })),
    el("p", { class: "set-hint" }, "System matches your computer, and switches when it does."),
    el("div", { class: "looks", role: "radiogroup", "aria-labelledby": "set-h-appearance" }, choices));
}

// A small drawing of the window in one theme: the rail, a title and a few lines, the gold button
function sketch(theme) {
  return el("span", { class: `look-win look-${theme}-win` },
    el("i", { class: "look-rail" }),
    el("span", { class: "look-page" },
      el("i", { class: "look-title" }), el("i", { class: "look-line" }), el("i", { class: "look-line short" }),
      el("i", { class: "look-button" })));
}

function show(choice) {
  if (current() !== choice) root.setAttribute("data-appearance", choice);
  for (const input of document.querySelectorAll('input[name="appearance"]')) input.checked = input.value === choice;
}

function choose(choice) {
  const before = current();
  picks += 1;
  show(choice);
  saving = saving.then(() => postJSON("/api/appearance", { appearance: choice })).then(() => {
    $("#appearance-note").textContent = "";
  }, (e) => {
    if (current() === choice) show(before);
    $("#appearance-note").textContent = `Not saved: ${e.message}`;
  });
}

async function refresh() {
  let settled;
  do {
    settled = saving;
    await settled;
  } while (settled !== saving);  // a pick made meanwhile is saved first
  const at = picks;
  try {
    const { appearance } = await getJSON("/api/appearance");
    if (at === picks && appearance && appearance !== current()) show(appearance);
  } catch (e) { /* Ixel stopped: this window keeps what it has */ }
}
