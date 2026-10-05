// Settings: choices among what's already set up (which models sit on the panel and how each one
// thinks, the default mode, Saver, Triage and pictures), model servers on your own computers, and the
// keys. Each model is picked from a list (its company's own when Ixel can ask, see model_choices.py),
// with Other… for any name. Each change is saved to the settings file as it's made; the file from before
// it is kept as config.toml.bak. Models on your own computers (Ollama, LM Studio…) can be added, pointed
// at another of your computers, or taken off here, never with a key. Everything else about what runs and
// where things are sent (a command, any other address, a key name) stays with `ixel setup` and the file
// itself. Keys only go one way: the page can save or remove one, never read it.

import { $, $$, el, icon, api, getJSON, postJSON } from "./common.js";

const MODE_WORDS = {
  quick: "Quick: answers and a verdict",
  review: "Review: answers, peer review, verdict",
  deep: "Deep: review, then revised answers",
  saver: "Saver: cheap drafts, one big check",
  auto: "Auto: Triage picks",
  compare: "Compare: answers side by side",
};
const ESCALATE_WORDS = { always: "Every time (safest)", disagreement: "Only when the drafts disagree (saves the most)" };
const ON_WRONG_WORDS = { send_back: "Sends it back to be fixed", correct: "Fixes it itself" };
const TRIAGE_WORDS = { typesafe: "TypeSafe's decision API", model: "One of your models" };
const KEY_STATES = { file: "Saved in Ixel", system: "Set outside Ixel", none: "Not set" };
const KINDS = { api: "API", cli: "Program", gateway: "Gateway" };
const NOTE_ICONS = { ok: "check", warn: "info", fail: "alert" };

let changed = () => {};
let data = null;          // what the server last said
let taken = 0;            // counts snapshots, so an older read never replaces a newer one
let failed = "";
let loading = null;
const notes = {};         // a line beside each group's title: saving, saved, or what went wrong
let confirming = "";      // the key whose Remove waits for a second press
let queue = Promise.resolve();
let pending = 0;
let epoch = 0;            // a refused change (the file changed) drops the changes queued behind it
let forgetTyped = false;  // the file changed: the next redraw shows it, not what was typed over it
let wantFocus = "";       // where focus goes after the next redraw, when its control is redrawn away
let lists = null;         // each model's list to pick from, once the server has asked around
let listsFailed = "";
let listsLoading = null;
let listsAgain = false;   // a key changed while the lists were being read: read them once more after
const listsAsked = new Set();  // models added since the lists were read, asked about once each
const naming = new Set(); // models whose Other… box is open, for a name that isn't in the list
const OTHER = "\u0000other";  // Other…'s value: no model name can hold it
let found = null;         // the model servers the looks so far found, newest first
let looking = "";         // where a look is under way: "here" or the address typed ("" when none)
let lookNext = "";        // a server to look at again once the look under way is done
let lookFailed = "";
let removing = "";        // the model whose Take off waits for a second press
let getting = null;       // a model an Ollama of yours is downloading: { base, model, text }
let forgetting = false;   // Forget waits for a second press

export function start(options) {
  if (options && options.changed) changed = options.changed;
  $("#settings-docs").addEventListener("click", openDocs);
}

// Ixel opens the docs in your own browser: a link here would open them in Ixel's window instead
let docsNoteTimer = 0;
async function openDocs() {
  const note = $("#settings-docs-note");
  clearTimeout(docsNoteTimer);
  let reply = {};
  try {
    reply = await postJSON("/api/docs", {});
  } catch (e) { /* Ixel stopped: say where the docs are */ }
  if (reply.opened) {
    note.textContent = "Opened in your browser";
    docsNoteTimer = setTimeout(() => { note.textContent = ""; }, 5000);
  } else {
    note.textContent = `The docs are at ${reply.url || "https://ixelai.com/docs/"}`;
  }
}

export function shown() {
  if (!pending) {
    for (const group of Object.keys(notes)) if (notes[group].kind !== "fail") delete notes[group];
    load();
  }
  loadLists();
  render();
}

function take(snapshot) {
  data = snapshot;
  taken += 1;
}

function load() {
  if (loading) return loading;
  const at = taken;
  loading = getJSON("/api/settings")
    .then((snapshot) => { if (taken === at) take(snapshot); failed = ""; })
    .catch((e) => { failed = e.message; })
    .finally(() => { loading = null; render(); });
  return loading;
}

// The lists come from each company, so they can take a few seconds: the page doesn't wait for them
function loadLists() {
  if (listsLoading) {
    listsAgain = true;
    return listsLoading;
  }
  listsLoading = getJSON("/api/settings/models")
    .then((reply) => { lists = reply.agents || {}; listsFailed = ""; })
    .catch((e) => { listsFailed = e.message; })
    .finally(() => {
      listsLoading = null;
      if (listsAgain) {
        listsAgain = false;
        loadLists();
      }
      render();
    });
  return listsLoading;
}

// ── Saving ──────────────────────────────────────────────────────────────────

async function post(path, body) {
  let res;
  let reply;
  try {
    res = await api(path, { method: "POST", body: JSON.stringify(body) });
    reply = await res.json().catch(() => ({}));
  } catch (e) {
    return { ok: false, error: "Can't reach Ixel. Is ixel gui still running?" };
  }
  if (reply && reply.settings) take(reply.settings);
  if (res.ok) return { ok: true, reply };
  return { ok: false, status: res.status, error: (reply && reply.error) || `Couldn't save (${res.status})` };
}

// One change at a time, each sent with the file's version as it stands once the one before is in.
// Keys don't depend on the settings file, so a refusal over the file never drops one.
function send(group, path, body, done) {
  const onFile = path === "/api/settings";
  const at = epoch;
  pending += 1;
  note(group, "Saving…", "busy");
  queue = queue.then(async () => {
    if (onFile && at !== epoch) {
      note(group, "Not saved, since the file changed. Make the change again.", "fail");
      return;
    }
    const result = await post(path, onFile ? { ...body, version: data.version } : body);
    if (onFile && !result.ok && result.status === 409) {
      epoch += 1;
      forgetTyped = true;
      naming.clear();
    }
    done(result);
  }).catch(() => {}).finally(() => {
    pending -= 1;
    if (!pending) render();
  }).catch(() => {});  // a redraw that failed mustn't stop the next change from being sent
}

function save(group, body, saved = () => {}) {
  send(group, "/api/settings", body, (result) => {
    if (result.ok) {
      note(group, "Saved", "ok");
      saved();
      changed();
    } else {
      note(group, result.error, "fail");
    }
  });
}

function saveKey(entry, input) {
  const value = input.value;
  input.value = "";  // the key leaves the page with this request and isn't kept
  if (!value.trim()) {
    note(`key:${entry.name}`, "Paste the key first.", "fail");
    return;
  }
  sendKey(entry, { name: entry.name, value });
}

function sendKey(entry, body) {
  send(`key:${entry.name}`, "/api/settings/key", body, (result) => {
    if (result.ok) {
      note(`key:${entry.name}`, result.reply.message || "Saved", result.reply.state === "system" ? "warn" : "ok");
      changed();
      loadLists();  // a new key can show that company's own list
    } else {
      note(`key:${entry.name}`, result.error, "fail");
    }
  });
}

function note(group, text, kind) {
  notes[group] = { text, kind };
  const node = $$("[data-note]", $("#settings-body")).find((n) => n.dataset.note === group);
  if (node) fillNote(node, group);  // in place, so a screen reader hears the change
}

function fillNote(node, group) {
  const n = notes[group] || { text: "", kind: "" };
  node.className = `set-note ${n.kind}`;
  node.replaceChildren(...[
    n.kind === "busy" ? el("span", { class: "spin" }) : n.text && NOTE_ICONS[n.kind] ? icon(NOTE_ICONS[n.kind]) : null,
    n.text ? el("span", {}, n.text) : null,
  ].filter(Boolean));
  return node;
}

function noteNode(group) {
  return fillNote(el("span", { "data-note": group, role: "status" }), group);
}

// ── Drawing ─────────────────────────────────────────────────────────────────

function render() {
  if ($("#view-settings").hidden || pending) return;
  const root = $("#settings-body");
  const active = document.activeElement;
  const focusKey = wantFocus || (root.contains(active) && active.dataset ? active.dataset.key || "" : "");
  const selection = !wantFocus && focusKey && typeof active.selectionStart === "number"
    ? [active.selectionStart, active.selectionEnd] : null;
  wantFocus = "";
  // Words typed but not saved yet (a model name being written, a key being pasted) outlive a redraw,
  // except when the file changed: then it's shown as it is, and a key being pasted is all that's kept
  const typed = {};
  for (const input of $$("input[data-key]", root)) {
    if (input.type === "checkbox" || input.value === input.defaultValue) continue;
    if (!forgetTyped || input.type === "password") typed[input.dataset.key] = input.value;
  }
  forgetTyped = false;

  $("#settings-where").textContent = data && data.source ? "Saved as you change them" : "";
  root.replaceChildren(...build().filter(Boolean));

  for (const input of $$("input[data-key]", root)) {
    if (Object.hasOwn(typed, input.dataset.key)) input.value = typed[input.dataset.key];
  }
  if (focusKey) {
    const target = $$("[data-key]", root).find((n) => n.dataset.key === focusKey);
    if (target && !target.disabled) {
      target.focus();
      if (selection) try { target.setSelectionRange(...selection); } catch (e) { /* not a text box */ }
    }
  }
}

function build() {
  if (!data) {
    if (failed) return [notice("error", `Couldn't read your settings: ${failed}`)];
    return [el("div", { class: "set-loading" }, el("span", { class: "spin" }), el("span", {}, "Reading your settings…"))];
  }
  return [
    data.source
      ? el("p", { class: "set-intro" }, "Changes are saved as you make them, to ", el("code", {}, data.source),
        data.backup ? ". The file from before each change is kept beside it as config.toml.bak. "
          : ". No copy of the old file is kept, since it has a key written in it. ",
        "To add a company's model or a program, use ", el("code", {}, "ixel setup"),
        "; models on your own computers can be added below.")
      : null,
    failed ? notice("error", `Couldn't read your settings again: ${failed}`) : null,
    data.problem ? notice("error", data.problem) : null,
    data.warnings.length ? notice("info", data.warnings) : null,
    models(),
    servers(),
    asking(),
    saver(),
    triage(),
    pictures(),
    soundCard(),
    keys(),
    kept(),
  ];
}

function notice(kind, text) {
  const lines = Array.isArray(text) ? text : [text];
  return el("div", { class: `notice ${kind}` }, icon(kind === "error" ? "alert" : "info"),
    el("div", {}, lines.map((line) => el("div", {}, line))));
}

function card(id, title, hint, ...rows) {
  return el("section", { class: "set-group", "aria-labelledby": `set-h-${id}` },
    el("div", { class: "set-head" }, el("h2", { id: `set-h-${id}` }, title), noteNode(id)),
    hint ? el("p", { class: "set-hint" }, hint) : null,
    el("div", { class: "set-list" }, rows.flat()));
}

// A labelled row with one control on the right
function row(label, hint, control) {
  return el("label", { class: "set-row" },
    el("span", { class: "set-text" }, el("span", { class: "set-label" }, label), hint ? el("small", {}, hint) : null),
    el("span", { class: "set-control" }, control));
}

function select(key, value, options, onChange, disabled = !data.editable) {
  const node = el("select", { "data-key": key, disabled },
    options.map(([v, text]) => el("option", { value: v, selected: v === value }, text)));
  node.addEventListener("change", () => onChange(node.value));
  return node;
}

function checkbox(key, checked, onChange, disabled = !data.editable, label = null) {
  const node = el("input", { type: "checkbox", "data-key": key, checked, disabled, "aria-label": label });
  node.addEventListener("change", () => onChange(node.checked));
  return node;
}

function textBox(key, value, attrs, onChange, disabled = !data.editable) {
  const node = el("input", { type: "text", "data-key": key, value, disabled, spellcheck: "false", autocomplete: "off", ...attrs });
  node.addEventListener("change", () => onChange(node.value.trim()));
  return node;
}

const agentChoices = (names = null) => data.agents.filter((a) => !names || names.includes(a.name)).map((a) => [a.name, a.label]);
const labelOf = (name) => (data.agents.find((a) => a.name === name) || {}).label || name;
const findKey = (key) => $$("[data-key]", $("#settings-body")).find((n) => n.dataset.key === key);
const cap = (word) => word.charAt(0).toUpperCase() + word.slice(1);

function models() {
  if (!data.agents.length) {
    return card("models", "Models", "", el("div", { class: "set-empty" }, "No models yet. Run ", el("code", {}, "ixel setup"),
      " in a terminal to add them, or add models on your own computers below."));
  }
  return card("models", "Models",
    "Which models answer, and which model and effort each one uses. Default leaves it to the provider; " +
    "Latest and the names under Keeps up by itself move to each new model as it comes out.",
    data.agents.map((a) => {
      const own = data.editable && a.in_file;
      return el("div", { class: "set-agent" },
        el("div", { class: "set-agent-head" },
          el("span", { class: "set-label" }, a.label),
          el("span", { class: "set-sub" }, `${KINDS[a.kind] || a.kind} · ${a.name}`),
          el("span", { class: "spacer" }),
          el("label", { class: "set-check" },
            checkbox(`agent:${a.name}:on`, a.on_panel, () => {
              const on = data.agents.filter((b) => {
                const box = findKey(`agent:${b.name}:on`);
                return box ? box.checked : b.on_panel;
              }).map((b) => b.name);
              save("models", { section: "panel", values: { on } });
            }, !data.editable, `${a.label} on the panel`),
            el("span", {}, "On the panel"))),
        el("div", { class: "set-agent-fields" },
          el("label", { class: "set-field" }, el("span", {}, "Model"), modelPicker(a, own)),
          naming.has(a.name) && own
            ? textBox(`agent:${a.name}:model-name`, "", { placeholder: "Model name", maxlength: "200",
              "aria-label": `${a.label} model name` }, (model) => {
              // The box stays until the name is saved, so one that's refused can be fixed
              if (model) {
                save("models", { section: "agent", agent: a.name, values: { model } }, () => {
                  naming.delete(a.name);
                  loadLists();
                });
              }
            })
            : null,
          effortsOf(a).length
            ? el("label", { class: "set-field" }, el("span", {}, "Effort"),
              labelled(select(`agent:${a.name}:effort`, a.effort, effortChoices(a),
                (effort) => save("models", { section: "agent", agent: a.name, values: { effort } }), !own), `${a.label} effort`))
            : null),
        a.server ? serverFields(a, own) : null,
        effortNote(a) ? el("small", { class: "set-sub" }, effortNote(a)) : null,
        a.in_file ? null : el("small", { class: "set-sub" }, "Not in your settings file, so its model can't be changed here."),
        a.in_file ? listLine(a) : null);
    }));
}

// The levels this model takes: for one that follows the newest, those of the model it is now, once known
function liveEfforts(a) {
  const list = lists && lists[a.name];
  return list && list.efforts && list.follows === a.model ? list : null;
}

function effortsOf(a) {
  const live = liveEfforts(a);
  return live ? live.efforts : a.efforts;
}

function effortNote(a) {
  const list = liveEfforts(a);
  if (!list) return a.effort_note;
  return list.efforts.length ? "" : `${list.resolved} has no effort setting, so Ixel sends none.`;
}

// The nearest level a model takes, as a question sends it (ties go to the lower one)
const LEVELS = ["minimal", "low", "medium", "high", "xhigh", "max"];
function nearest(level, levels) {
  const at = LEVELS.indexOf(level);
  return levels.reduce((best, l) => {
    const d = Math.abs(LEVELS.indexOf(l) - at);
    const b = Math.abs(LEVELS.indexOf(best) - at);
    return d < b || (d === b && LEVELS.indexOf(l) < LEVELS.indexOf(best)) ? l : best;
  });
}

// A model on a server of yours: where the server is (another of your computers can be typed in), and Take off
function serverFields(a, own) {
  const confirmingThis = removing === a.name;
  return el("div", { class: "set-agent-fields" },
    el("label", { class: "set-field" }, el("span", {}, "Server"),
      textBox(`agent:${a.name}:url`, a.server.url, { maxlength: "300", "aria-label": `${a.label} server address` },
        (url) => {
          if (url && url !== a.server.url) save("models", { section: "agent", agent: a.name, values: { url } });
        }, !own)),
    confirmingThis
      ? [el("span", { class: "set-confirm" }, `Take ${a.label} off your list?`),
        el("button", { type: "button", class: "btn danger", "data-key": `agent:${a.name}:confirm`, onclick: () => {
          removing = "";
          save("models", { section: "remove_model", agent: a.name });
        } }, "Take off"),
        el("button", { type: "button", class: "btn", "data-key": `agent:${a.name}:keep`, onclick: () => {
          removing = "";
          wantFocus = `agent:${a.name}:remove`;
          render();
        } }, "Keep it")]
      : el("button", { type: "button", class: "btn", "data-key": `agent:${a.name}:remove`, disabled: !own,
        "aria-label": `Take ${a.label} off your list`, onclick: () => {
          removing = a.name;
          wantFocus = `agent:${a.name}:confirm`;
          render();
        } }, "Take off"));
}

// ── Models on your computers ────────────────────────────────────────────────

const sameServer = (a, b) => {
  const norm = (url) => url.toLowerCase().replace(/\/+$/, "").replace("://localhost:", "://127.0.0.1:");
  return norm(a) === norm(b);
};
// On your list: as the server said when it looked, or added since
const isAdded = (server, model) => data.agents.some((a) => (server.added && server.added[model] === a.name)
  || (a.server && sameServer(a.server.url, server.base) && (a.model === model || `${a.model}:latest` === model)));

// Asks the server to look on this computer (address "") or at the address typed; nothing is saved
async function look(address) {
  if (looking) {
    if (address) lookNext = address;  // after this one: a server whose list just changed
    return;
  }
  looking = address || "here";
  lookFailed = "";
  render();
  try {
    const reply = await postJSON("/api/settings/servers", address ? { address } : {});
    const now = reply.servers || [];
    found = [...now, ...(found || []).filter((s) => !now.some((n) => sameServer(n.base, s.base)))];
  } catch (e) {
    lookFailed = e.message;
  } finally {
    looking = "";
    render();
  }
  if (lookNext) {
    const next = lookNext;
    lookNext = "";
    look(next);
  }
}

function servers() {
  if (!data.can_add) return null;
  const busy = Boolean(looking);
  const address = el("input", { type: "text", "data-key": "servers:address", placeholder: "mac-mini or 192.168.1.20:1234",
    spellcheck: "false", autocomplete: "off", maxlength: "300", disabled: busy,
    "aria-label": "Another computer's name or address" });
  address.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (address.value.trim()) look(address.value.trim());
    }
  });
  return card("servers", "Models on your computers",
    "Ollama, LM Studio and other servers that run models on your own computers. Ixel looks only when you press " +
    "Look: on this computer at each server's usual port, or at the address you type, never around your network. " +
    "They're sent no key, and your questions stay on your computers.",
    el("div", { class: "set-row" },
      el("span", { class: "set-text" }, el("span", { class: "set-label" }, "This computer")),
      el("span", { class: "set-control" },
        el("button", { type: "button", class: "btn", "data-key": "servers:here", disabled: busy, onclick: () => look("") },
          looking === "here" ? "Looking…" : "Look"))),
    el("div", { class: "set-row" },
      el("span", { class: "set-text" }, el("span", { class: "set-label" }, "Another computer"),
        el("small", {}, "Its name, address or Tailscale name. The server there has to let other computers in: in LM " +
          "Studio, turn on Serve on Local Network; for Ollama, set OLLAMA_HOST=0.0.0.0 and restart it. Out of the box " +
          "neither asks for a password, so anyone on that network can then use it: only do this on a computer that " +
          "stays on your home network.")),
      el("span", { class: "set-control set-pair" }, address,
        el("button", { type: "button", class: "btn", "data-key": "servers:there", disabled: busy,
          onclick: () => { if (address.value.trim()) look(address.value.trim()); } },
        looking && looking !== "here" ? "Looking…" : "Look"))),
    lookFailed
      ? el("div", { class: "set-row" }, el("span", { class: "set-text" }, el("small", { class: "set-warn" }, lookFailed)))
      : null,
    (found || []).map(serverRow));
}

function serverRow(server) {
  const title = `${server.name} on ${server.where}`;
  return el("div", { class: "set-agent", role: "group", "aria-label": title },
    el("div", { class: "set-agent-head" }, el("span", { class: "set-label" }, title), el("code", { class: "set-sub" }, server.base)),
    server.models.length
      ? el("div", { class: "set-models" }, server.models.map((model) => el("div", { class: "set-key-row" },
        el("span", { class: "set-model-name" }, model),
        isAdded(server, model)
          ? el("span", { class: "set-state file" }, "On your list")
          : el("button", { type: "button", class: "btn", "data-key": `servers:add:${server.base}:${model}`,
            "aria-label": `Add ${model} from ${title}`, onclick: () => add(server, model) }, "Add"))))
      : el("small", { class: "set-sub" }, "It has no model that answers questions yet."),
    server.hidden.length
      ? el("small", { class: "set-sub" }, `Left out, since they can't answer questions: ${server.hidden.join(", ")}.`)
      : null,
    (server.elsewhere || []).length
      ? el("small", { class: "set-sub" }, `Left out, since they run on ollama.com, not your computer: ${server.elsewhere.join(", ")}.`)
      : null,
    server.ollama ? getModel(server, title) : null);
}

// Ollama can download a model by name (Ollama does it, from ollama.com, onto the computer it runs on)
function getModel(server, title) {
  const mine = getting && getting.base === server.base;
  const name = el("input", { type: "text", "data-key": `servers:get:${server.base}`, placeholder: "qwen3:8b",
    spellcheck: "false", autocomplete: "off", maxlength: "200", disabled: Boolean(getting),
    "aria-label": `A model for ${title} to get` });
  const go = () => { if (name.value.trim()) get(server, name.value.trim()); };
  name.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      go();
    }
  });
  return el("div", { class: "set-get" },
    el("div", { class: "set-key-row" },
      el("span", { class: "set-model-name" }, "Get a model"),
      el("span", { class: "set-control set-pair" }, name,
        el("button", { type: "button", class: "btn", "data-key": `servers:get-go:${server.base}`, disabled: Boolean(getting),
          onclick: go }, mine ? "Getting…" : "Get"))),
    el("small", { class: "set-sub" },  // (not a live region: it changes a few times a second)
      mine ? getting.text : "By its name on ollama.com/library (or hf.co/… for Hugging Face). Ollama downloads it " +
        "onto that computer."));
}

const size = (bytes) => bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(bytes / 1e6))} MB`;

async function get(server, model) {
  if (getting) return;
  getting = { base: server.base, model, text: `Asking Ollama for ${model}…` };
  note("servers", `Getting ${model} onto ${server.where}…`, "");
  render();
  let failed = "";
  let done = false;
  try {
    const res = await api("/api/settings/servers/pull", { method: "POST", body: JSON.stringify({ base: server.base, model }) });
    if (!res.ok) {
      failed = (await res.json().catch(() => ({}))).error || `Request failed (${res.status})`;
    } else {
      await readLines(res, ({ kind, data }) => {
        if (kind === "progress") {
          const part = data.total ? ` ${Math.floor((100 * data.completed) / data.total)}% of ${size(data.total)}` : "";
          getting.text = `${model}: ${data.status || "working"}${part}`;
          const line = findKey(`servers:get:${server.base}`);
          const status = line && line.closest(".set-get").querySelector("small");
          if (status) status.textContent = getting.text;  // no redraw for each line: the page stays put
        } else if (kind === "done") {
          done = true;
        } else if (kind === "error") {
          failed = data.message;
        }
      });
    }
  } catch (e) {
    failed = e.message;
  } finally {
    getting = null;
  }
  if (done) {
    note("servers", `${model} is on ${server.where} now. Add it below to put it on the panel.`, "ok");
    look(server.base);  // that server again, for its list now
  } else {
    note("servers", failed || `Ollama stopped before ${model} was ready. Get it again to carry on.`, "fail");
    render();
  }
}

async function readLines(res, onLine) {
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
      if (line) onLine(JSON.parse(line));
    }
  }
  if (buffer.trim()) onLine(JSON.parse(buffer));
}

function add(server, model) {
  send("servers", "/api/settings", { section: "add_model", values: { base: server.base, model } }, (result) => {
    if (result.ok) {
      note("servers", result.reply.message || "Added", "ok");
      changed();
    } else {
      note("servers", result.error, "fail");
    }
  });
}

// Only the levels this model takes; one saved before that it doesn't take says what's sent instead
function effortChoices(a) {
  const levels = effortsOf(a);
  const choices = [["", "Default"], ...levels.map((e) => [e, cap(e)])];
  if (a.effort && !levels.includes(a.effort)) choices.push([a.effort, `${cap(a.effort)} (sent as ${cap(nearest(a.effort, levels))})`]);
  return choices;
}

const ALIAS_WORDS = { latest: "Latest", "latest-fast": "Latest fast" };

// Default, the names that keep up by themselves, the list, and Other… for any name
function modelPicker(a, own) {
  const list = lists && lists[a.name];
  const current = a.model;
  const choosing = naming.has(a.name) && own;
  const known = new Set();
  const option = (value, text) => {
    known.add(value);
    return el("option", { value, selected: !choosing && value === current }, text);
  };
  if (lists && !list && !listsAsked.has(a.name)) {  // a model added since the lists were read
    listsAsked.add(a.name);
    loadLists();
  }
  let first = "Default";
  if (a.needs_model) first = "Not set: pick one";
  else if (list && list.newest) first = list.default ? `Default (now ${list.default})` : "Default (the newest)";
  else if (a.kind === "cli") first = `Default (${a.label}'s own)`;
  const top = a.needs_model && current ? [] : [option("", first)];
  const follow = list && list.names.length
    ? el("optgroup", { label: "Keeps up by itself" }, list.names.map((n) =>
      option(n.id, ALIAS_WORDS[n.id] ? `${ALIAS_WORDS[n.id]} (${n.now || n.about})` : `${n.id} (${n.about})`)))
    : null;
  const listed = list && list.models.length
    ? el("optgroup", { label: list.source === "built_in" ? "Built in (may be out of date)" : `From ${list.where}` },
      list.models.filter((m) => !known.has(m)).map((m) => option(m, m)))
    : null;
  const mine = current && !known.has(current) ? option(current, `${current} (your setting)`) : null;
  const node = el("select", { "data-key": `agent:${a.name}:model`, disabled: !own, class: "set-model",
    "aria-label": `${a.label} model` },
  top, mine, follow, listed,
  !list && !listsFailed ? el("option", { disabled: true, value: "\u0000looking" }, "Looking up models…") : null,
  el("option", { value: OTHER, selected: choosing }, "Other…"));
  node.addEventListener("change", () => {
    if (node.value === OTHER) {
      naming.add(a.name);
      wantFocus = `agent:${a.name}:model-name`;
      render();
      return;
    }
    naming.delete(a.name);
    // A model that follows the newest gets the levels of the one it is now with the lists
    save("models", { section: "agent", agent: a.name, values: { model: node.value } }, loadLists);
  });
  return node;
}

// Where a model's list came from, and how to get its company's own when it's the built-in one
function listLine(a) {
  if (listsFailed) return el("small", { class: "set-sub" }, `Couldn't look up the models to pick from: ${listsFailed}`);
  const list = lists && lists[a.name];
  if (!list) return null;
  const from = { company: `Models from ${list.where}'s own list.`, program: `Models from ${list.where}'s own list.`,
    server: `Models from ${list.where}.`, built_in: "Ixel's built-in list, which may be out of date." }[list.source];
  const text = [from, list.note].filter(Boolean).join(" ");
  return text ? el("small", { class: "set-sub" }, text) : null;
}

function labelled(node, label) {
  node.setAttribute("aria-label", label);
  return node;
}

function asking() {
  const r = data.review;
  // Only a model on the panel can write the verdict (and the one that does now, if it isn't)
  const onPanel = data.agents.filter((a) => a.on_panel || a.name === r.moderator).map((a) => a.name);
  return card("review", "Asking", "",
    row("Start on", "The mode Ask starts on, and what ixel ask runs when you don't name one.",
      select("review:mode", r.mode, data.choices.modes.map((m) => [m, MODE_WORDS[m] || m]),
        (mode) => save("review", { section: "review", values: { mode } }))),
    el("label", { class: "set-row set-toggle" },
      el("span", { class: "set-text" }, el("span", { class: "set-label" }, "Private"), privateHint()),
      el("span", { class: "set-control" },
        checkbox("review:private", r.private, (value) => save("review", { section: "review", values: { private: value } })))),
    row("Who writes the verdict", "One of the models on the panel.",
      select("review:moderator", r.moderator, [["", "The author of the best-rated answer"], ...agentChoices(onPanel)],
        (moderator) => save("review", { section: "review", values: { moderator } }))),
    row("A question typed in the terminal", "What ixel's chat runs for a question typed without a /command.",
      select("review:plain", r.plain_questions, data.choices.plain.map((m) => [m, MODE_WORDS[m] || m]),
        (value) => save("review", { section: "review", values: { plain_questions: value } }))),
    row("Time limit", "Seconds each model gets for one answer, from 10 to 3600.",
      textBox("review:timeout", String(r.timeout), { inputmode: "numeric", class: "set-number" }, (text) => {
        const timeout = Number(text);
        if (!/^\d+(\.\d+)?$/.test(text)) {
          note("review", "The time limit must be a number of seconds.", "fail");
          return;
        }
        save("review", { section: "review", values: { timeout } });
      })));
}

// Private: which of the panel's models answer, and which sit out
function privateHint() {
  const panel = data.agents.filter((a) => a.on_panel);
  const mine = panel.filter((a) => a.yours);
  const out = panel.filter((a) => !a.yours).map((a) => a.label);
  if (!mine.length) {
    return el("small", { class: data.review.private ? "set-warn" : "" }, "Only models on your own computers answer, " +
      "and none on the panel does yet. Add one above, under Models on your computers.");
  }
  return el("small", {}, "Only models on your own computers answer, so questions, answers and sound stay with you." +
    (out.length ? ` ${data.review.private ? "Sitting out" : "Would sit out"}: ${out.join(", ")}.` : ""));
}

// Every model but the verifier drafts, unless the file names which ones do
const drafters = () => data.agents.filter((a) => a.name !== data.saver.verifier);
const drafting = (a) => !data.saver.drafters.length || data.saver.drafters.includes(a.name);

function saver() {
  const s = data.saver;
  const moderator = data.review.moderator;
  const others = drafters();
  return card("saver", "Saver", "Cheaper models draft and check each other; one big model only verifies their work.",
    row("Big model that verifies", s.verifier ? "" : "Saver mode can't run until one is picked.",
      select("saver:verifier", s.verifier_set,
        [["", moderator ? `The one that writes the verdict (${labelOf(moderator)})` : "Not picked"], ...agentChoices()],
        (verifier) => save("saver", { section: "saver", values: { verifier } }))),
    others.length
      ? el("div", { class: "set-row set-many", role: "group", "aria-label": "Models that draft" },
        el("span", { class: "set-text" }, el("span", { class: "set-label" }, "Models that draft")),
        el("span", { class: "set-checks" }, others.map((a) => el("label", { class: "set-check" },
          checkbox(`saver:draft:${a.name}`, drafting(a), () => {
            const now = drafters();  // as the file says now, not as it was drawn
            const on = now.filter((b) => {
              const box = findKey(`saver:draft:${b.name}`);
              return box ? box.checked : drafting(b);
            }).map((b) => b.name);
            save("saver", { section: "saver", values: { drafters: on.length === now.length ? null : on } });
          }),
          el("span", {}, a.label)))))
      : null,
    row("Call the verifier", "",
      select("saver:escalate", s.escalate, data.choices.escalate.map((v) => [v, ESCALATE_WORDS[v] || v]),
        (escalate) => save("saver", { section: "saver", values: { escalate } }))),
    row("When a draft is wrong, the verifier", "",
      select("saver:on_wrong", s.on_wrong, data.choices.on_wrong.map((v) => [v, ON_WRONG_WORDS[v] || v]),
        (value) => save("saver", { section: "saver", values: { on_wrong: value } }))));
}

function triage() {
  const t = data.triage;
  let state = "";
  if (t.enabled && !t.ready) {
    state = t.provider === "model" ? "Not ready: pick the model that decides."
      : `Not ready: save a key for ${t.key} under Keys.`;
  } else if (t.enabled && t.provider === "typesafe" && !t.official && t.host) {
    state = `Triage's questions go to ${t.host}, not TypeSafe's own API.`;
  }
  return card("triage", "Triage", "A quick check that decides how much checking a question needs.",
    el("label", { class: "set-row set-toggle" },
      el("span", { class: "set-text" }, el("span", { class: "set-label" }, "Use Triage"),
        state ? el("small", { class: "set-warn" }, state) : null),
      el("span", { class: "set-control" },
        checkbox("triage:enabled", t.enabled, (enabled) => save("triage", { section: "triage", values: { enabled } })))),
    row("Who decides", t.provider === "typesafe" ? "Your question goes to TypeSafe too." : "",
      select("triage:provider", t.provider, data.choices.triage_providers.map((p) => [p, TRIAGE_WORDS[p] || p]),
        (provider) => save("triage", { section: "triage", values: { provider } }))),
    t.provider === "model"
      ? row("The model that decides", "A fast, cheap one is best.",
        select("triage:agent", t.agent, [...(t.agent ? [] : [["", "Not picked"]]), ...agentChoices()],
          (agent) => save("triage", { section: "triage", values: { agent } })))
      : null,
    el("label", { class: "set-row set-toggle" },
      el("span", { class: "set-text" }, el("span", { class: "set-label" }, "Offer Auto mode"),
        el("small", {}, "Triage picks Quick, Review or Deep for each question.")),
      el("span", { class: "set-control" },
        checkbox("triage:auto", t.auto_mode, (value) => save("triage", { section: "triage", values: { auto_mode: value } })))),
    el("label", { class: "set-row set-toggle" },
      el("span", { class: "set-text" }, el("span", { class: "set-label" }, "Skip review when the answers agree"),
        el("small", {}, "Saves a round when every first answer already agrees.")),
      el("span", { class: "set-control" },
        checkbox("triage:skip", t.skip_review, (value) => save("triage", { section: "triage", values: { skip_review: value } })))));
}

function pictures() {
  const seeing = data.agents.filter((a) => a.can_see);
  return card("images", "Pictures", "",
    seeing.length
      ? el("div", { class: "set-row set-many", role: "group", "aria-label": "Models that see pictures" },
        el("span", { class: "set-text" }, el("span", { class: "set-label" }, "Models that see pictures"),
          el("small", {}, "Pictures you attach in Ask go only to these. Programs such as Claude Code and Codex never " +
            "get them. Turn one off if its model only reads text.")),
        el("span", { class: "set-checks" }, seeing.map((a) => el("label", { class: "set-check" },
          checkbox(`agent:${a.name}:pictures`, a.pictures,
            (value) => save("images", { section: "agent", agent: a.name, values: { pictures: value } }),
            !(data.editable && a.in_file)),
          el("span", {}, a.label)))))
      : null,
    row("Picture service", "What ixel image uses when you don't name one.",
      select("images:provider", data.images.provider,
        [["", "The first one with a key"], ...data.choices.image_providers.map((p) => [p.name, p.label])],
        (provider) => save("images", { section: "images", values: { provider } }))));
}

function soundCard() {
  const using = data.sound.using;
  return card("sound", "Sound", "",
    row("Sound service",
      using ? `Sound you record or attach in Ask goes to ${using} to be written out into your question. ` +
        "No model hears it, and Ixel doesn't keep it."
        : data.sound.problem || "Writing out sound needs an OpenAI or Groq key. Add one below, under Keys.",
      select("sound:provider", data.sound.provider,
        [["", "The first one with a key"], ...data.choices.sound_providers.map((p) => [p.name, p.label])],
        (provider) => save("sound", { section: "sound", values: { provider } }))));
}

function keys() {
  const store = data.key_store;
  const section = card("keys", "Keys",
    `${store.where} A saved key is never shown again, here or anywhere. ` +
    "A key set in your system or shell wins over one saved here.",
    data.keys.map(keyRow));
  // Saved keys that can't be opened (the keychain is locked, or its key is gone): above the list, in red
  if (store.problem) section.querySelector(".set-list").before(notice("error", store.problem));
  return section;
}

// One key's row, which can be drawn again on its own (Remove's question works while a save is under way)
function redrawKey(k, focus) {
  const old = $$(".set-key", $("#settings-body")).find((n) => n.dataset.row === k.name);
  if (old) old.replaceWith(keyRow(k));
  const target = findKey(focus);
  if (target) target.focus();
}

function keyRow(k) {
  const input = el("input", {
    type: "password", "data-key": `key:${k.name}:value`, autocomplete: "off", spellcheck: "false",
    placeholder: k.state === "none" ? "Paste the key" : "Paste a new key", "aria-label": `${k.label} key`,
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      saveKey(k, input);
    }
  });
  const removing = confirming === k.name;
  return el("div", { class: "set-key", "data-row": k.name, role: "group", "aria-label": `${k.label} key` },
    el("div", { class: "set-agent-head" },
      el("span", { class: "set-label" }, k.label),
      el("code", { class: "set-sub" }, k.name),
      k.used_by.length ? el("span", { class: "set-sub" }, `used by ${k.used_by.join(", ")}`) : null,
      el("span", { class: "spacer" }),
      el("span", { class: `set-state ${k.state}` }, KEY_STATES[k.state] || k.state)),
    k.state === "system" && k.saved
      ? el("small", { class: "set-sub" }, "A copy saved in Ixel isn't used while that one is set.") : null,
    removing
      ? el("div", { class: "set-key-row" },
        el("span", { class: "set-confirm" }, `Remove ${k.label}'s key from Ixel?`),
        el("button", { type: "button", class: "btn danger", "data-key": `key:${k.name}:confirm`, onclick: () => {
          confirming = "";
          wantFocus = `key:${k.name}:value`;
          sendKey(k, { name: k.name, remove: true });
        } }, "Remove"),
        el("button", { type: "button", class: "btn", "data-key": `key:${k.name}:keep`, onclick: () => {
          confirming = "";
          redrawKey(k, `key:${k.name}:remove`);
        } }, "Keep it"))
      : el("div", { class: "set-key-row" },
        input,
        el("button", { type: "button", class: "btn", "data-key": `key:${k.name}:save`, "aria-label": `Save ${k.label}'s key`,
          onclick: () => saveKey(k, input) }, "Save"),
        k.saved
          ? el("button", { type: "button", class: "btn danger", "data-key": `key:${k.name}:remove`,
            "aria-label": `Remove ${k.label}'s key`, onclick: () => {
              confirming = k.name;
              redrawKey(k, `key:${k.name}:confirm`);
            } }, "Remove")
          : null),
    noteNode(`key:${k.name}`));
}

// What Ixel keeps of what you asked and ran, and Forget. The window's own storage is in use here, so
// `ixel forget` clears that, with Ixel closed.
function kept() {
  const ask = (now) => {
    forgetting = now;
    wantFocus = now ? "kept:confirm" : "kept:forget";
    render();
  };
  return card("kept", "What Ixel keeps",
    ["Your last few questions to ", el("code", {}, "ixel review"), " and its answers, each for a day, so ",
      el("code", {}, "--continue"), " can pick them up. The Machines log, for 30 days, without the commands you " +
      "run. Your keys, settings, machines and usage stats aren't part of this."],
    el("div", { class: "set-row" },
      el("span", { class: "set-text" }, el("span", { class: "set-label" }, "Forget them now"),
        el("small", {}, "To clear this window's own storage too, close Ixel and run ", el("code", {}, "ixel forget"),
          ".")),
      forgetting
        ? el("span", { class: "set-control set-pair" },
          el("span", { class: "set-confirm" }, "Delete them?"),
          el("button", { type: "button", class: "btn danger", "data-key": "kept:confirm", onclick: forgetNow }, "Forget"),
          el("button", { type: "button", class: "btn", "data-key": "kept:keep", onclick: () => ask(false) }, "Keep them"))
        : el("span", { class: "set-control" },
          el("button", { type: "button", class: "btn danger", "data-key": "kept:forget", onclick: () => ask(true) },
            "Forget"))));
}

async function forgetNow() {
  forgetting = false;
  wantFocus = "kept:forget";
  render();
  note("kept", "Deleting…", "busy");
  const result = await post("/api/forget", {});
  if (!result.ok) {
    note("kept", result.error, "fail");
    return;
  }
  const found = result.reply.forgotten || [];
  const failed = found.filter((item) => item.error);
  const gone = [...new Set(found.filter((item) => !item.error).map((item) => item.what))];
  if (failed.length) {
    note("kept", `Couldn't delete ${failed[0].what}: ${failed[0].error}`, "fail");
  } else if (gone.length) {
    note("kept", `Deleted ${gone.join(" and ")}`, "ok");
  } else {
    note("kept", "Nothing to forget", "ok");
  }
}
