// Machines: your servers over SSH, and the OpenClaw or Hermes agent on each. Connect opens a terminal;
// "Run on machines" runs one command you type on the ones you pick. Each server's key is pinned the first
// time (you see its fingerprint and say yes), and ssh then refuses a server whose key changed. Nothing
// here runs unless you typed or picked it.

import { $, el, icon, copyButton, getJSON, postJSON, plural } from "./common.js";

let data = null;      // GET /api/machines
let failed = "";
let loading = null;
let run = null;       // the run on screen: GET /api/machines/run
let runOpen = false;  // the Run form is showing
let runDraft = { command: "", timeout: 120, ids: null };
const expanded = new Set();  // machines whose full output is open in the run
let polling = null;

const STATE_WORDS = {
  waiting: "Waiting", running: "Running", ok: "Done", failed: "Failed", error: "Couldn't run",
  timeout: "Timed out", stopped: "Stopped",
};
const STATE_ICONS = { waiting: "circle", running: "refresh", ok: "check", failed: "x", error: "alert", timeout: "alert", stopped: "stop" };

export function start() {
  $("#machines-add").addEventListener("click", () => editMachine(null));
  $("#machines-import").addEventListener("click", importMenu);
  $("#machines-run-open").addEventListener("click", () => { runOpen = !runOpen; render(); if (runOpen) focusRun(); });
}

export function shown() {
  load();
}

export function hidden() {
  stopPolling();
}

async function load() {
  if (loading) return loading;
  loading = getJSON("/api/machines")
    .then((d) => {
      data = d;
      failed = "";
      // A run still going (started before a reload, or in another window) comes back on screen, with Stop
      const going = (d.runs || []).find((r) => r.running);
      if (going && !(run && run.id === going.id)) { run = going; expanded.clear(); }
    })
    .catch((e) => { failed = e.message; })
    .finally(() => {
      loading = null;
      render();
      if (run && run.running) poll();  // also picks up one that ended while the page was hidden
    });
  render();
  return loading;
}

// ── The page ──────────────────────────────────────────────────────────────

function render() {
  if ($("#view-machines").hidden) return;
  const body = $("#machines-body");
  const count = data ? data.machines.length : 0;
  $("#machines-sub").textContent = data && count ? plural(count, "machine") : "";
  $("#machines-run-open").hidden = !count;
  $("#machines-run-open").setAttribute("aria-expanded", String(runOpen));
  $("#machines-import").hidden = !data || !importsAvailable();

  if (failed) {
    body.replaceChildren(notice("error", `Couldn't read your machines: ${failed}`));
    return;
  }
  if (!data) {
    body.replaceChildren(el("div", { class: "set-loading" }, el("span", { class: "spin" }), "Looking…"));
    return;
  }
  const parts = [];
  if (data.problem) parts.push(notice("error", data.problem));
  if (!data.ssh) {
    parts.push(notice("warn", "ssh isn't installed here, or isn't on PATH. On Windows, add it in Settings > System > "
      + "Optional features > OpenSSH Client; on a Mac it's built in; on Linux, install openssh-client."));
  }
  if (data.unreadable) {
    parts.push(notice("warn", `${plural(data.unreadable, "saved machine")} in machines.json couldn't be read. `
      + "Ixel left them in the file as they are."));
  }
  if (!count) {
    parts.push(emptyState());
    body.replaceChildren(...parts);
    return;
  }
  if (runOpen) parts.push(runForm());
  if (run) parts.push(runResults());
  parts.push(...groups());
  parts.push(el("p", { class: "fine machines-foot" }, icon("lock"),
    el("span", {}, "Each server's key is pinned in Ixel's own file the first time, and ssh refuses a server whose "
      + "key has changed. Every connection and run is logged on this computer (machines.log, next to your settings).")));
  body.replaceChildren(...parts);
}

function notice(kind, text) {
  return el("div", { class: `board-notice ${kind === "error" ? "error" : ""}`, role: kind === "error" ? "alert" : "status" },
    icon("alert"), el("span", {}, text));
}

function importsAvailable() {
  const im = data.imports;
  return im.ssh_config > 0 || (im.console && im.console.machines > 0);
}

function emptyState() {
  const im = data.imports;
  const buttons = [el("button", { type: "button", class: "btn primary", onclick: () => editMachine(null) },
    icon("plus"), el("span", {}, "Add a machine"))];
  if (im.ssh_config) {
    buttons.push(el("button", { type: "button", class: "btn", onclick: () => bringIn("ssh_config") },
      `Import ${plural(im.ssh_config, "host")} from ~/.ssh/config`));
  }
  if (im.console && im.console.machines) {
    buttons.push(el("button", { type: "button", class: "btn", onclick: () => bringIn("console") },
      `Import ${plural(im.console.machines, "machine")} from Ixel Console`));
  }
  return el("section", { class: "board-message machines-empty" },
    el("div", { class: "board-message-icon" }, icon("server")),
    el("h2", {}, "Your machines"),
    el("p", {}, "Save your servers here, from a $5 VPS to the Mac mini in the closet, and open a terminal on one "
      + "with a click: a shell, or the OpenClaw or Hermes agent running there. Or run one command on several at once."),
    el("p", { class: "fine" }, "It runs only what you type or pick. The models never run anything here."),
    el("div", { class: "machines-empty-buttons" }, ...buttons));
}

function groups() {
  const byGroup = new Map();
  for (const m of data.machines) {
    const g = m.group || "";
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g).push(m);
  }
  const names = [...byGroup.keys()].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b));
  const titled = names.length > 1 || names[0] !== "";
  return names.map((g) => el("section", { class: "machine-group", "aria-label": g || "Machines" },
    titled ? el("h2", { class: "machine-group-title" }, g || "Other") : null,
    el("ul", { class: "machines" }, byGroup.get(g).map(machineRow))));
}

function runsWhat(m) {
  if (m.agent === "shell") return "a shell";
  if (m.agent === "custom") return m.command;
  return m.command;
}

function machineRow(m) {
  const pinned = m.pin.state === "pinned";
  const where = [m.address];
  if (m.jump) where.push(`through ${m.jump}`);
  return el("li", { class: "machine" },
    el("div", { class: "machine-main" },
      el("div", { class: "machine-top" },
        el("span", { class: "machine-name" }, m.name),
        el("span", { class: `pill ${pinned ? "key-pinned" : "key-new"}`, title: pinned ? m.pin.fingerprints.join("\n") : "" },
          pinned ? "Key pinned" : "Key not checked")),
      el("div", { class: "machine-sub" },
        el("span", { class: "mono" }, where.join(" ")),
        el("span", { "aria-hidden": "true" }, " · "),
        el("span", {}, `runs ${runsWhat(m)}`)),
      m.notes ? el("div", { class: "machine-notes" }, m.notes) : null),
    el("div", { class: "machine-buttons" },
      pinned
        ? el("button", { type: "button", class: "btn primary", onclick: (e) => connect(m, e.currentTarget) },
          icon("terminal"), el("span", {}, "Connect"))
        : el("button", { type: "button", class: "btn primary", onclick: (e) => checkKey(m, e.currentTarget) },
          icon("lock"), el("span", {}, "Check key")),
      el("button", { type: "button", class: "btn", onclick: () => editMachine(m) }, "Edit")));
}

// ── Doing things ──────────────────────────────────────────────────────────

async function busy(button, work) {
  if (button) {
    if (button.disabled) return undefined;
    button.disabled = true;
    button.classList.add("busy");
  }
  try {
    return await work();
  } finally {
    if (button) {
      button.disabled = false;
      button.classList.remove("busy");
    }
  }
}

function toast(title, text, kind) {
  const box = el("div", { class: `toast ${kind || ""}`, title: "Click to dismiss", role: "status" },
    icon(kind === "error" ? "alert" : "check"),
    el("div", {}, el("b", {}, title), text ? el("span", {}, text) : null));
  box.addEventListener("click", () => box.remove());
  $("#toasts").append(box);
  setTimeout(() => box.remove(), kind === "error" ? 12000 : 6000);
}

async function connect(m, button) {
  await busy(button, async () => {
    try {
      const done = await postJSON("/api/machines/connect", { id: m.id });
      toast(`Opened ${m.name}`, `In ${done.terminal}.`);
    } catch (e) {
      if (e.code === "not_pinned" || e.code === "hostkey") {
        await load();
        await checkKey(m, null);
      } else if (e.code === "no_terminal") {
        await infoDialog("No terminal to open", e.message, e.data && e.data.line);
      } else {
        toast(`Couldn't connect to ${m.name}`, e.message, "error");
      }
    }
  });
}

async function checkKey(m, button) {
  let seen;
  try {
    seen = await busy(button, () => postJSON("/api/machines/key", { id: m.id }));
  } catch (e) {
    toast(`Couldn't check ${m.name}'s key`, e.message, "error");
    return;
  }
  if (!seen) return;
  if (seen.state === "pinned") {
    toast(`${m.name}'s key matches`, "It's the key pinned for it.");
    await load();
    return;
  }
  if (seen.state === "changed") {
    await keyChanged(m, seen);
    return;
  }
  await trustDialog(m, seen);
}

async function trustDialog(m, seen) {
  const problem = el("div", { class: "dialog-problem", role: "alert" });
  const form = el("form", { method: "dialog" },
    el("h2", {}, `Is this ${m.name}?`),
    el("p", {}, `This is the first time Ixel connects to ${seen.address}${seen.jump ? ` (through ${seen.jump})` : ""}. `
      + "Its key says:"),
    el("div", { class: "fingerprint" }, el("code", {}, seen.fingerprint), el("span", {}, seen.key_type)),
    el("p", { class: "fine" }, "To be sure, compare it with the server's own: on the server, run ",
      el("code", {}, "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub"),
      ". Once it's pinned, ssh only connects to a server with this key."),
    problem,
    dialogButtons("Trust and pin it"));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const buttons = [...form.querySelectorAll("button")];
    if (buttons.some((b) => b.disabled)) return;
    buttons.forEach((b) => { b.disabled = true; });
    try {
      await postJSON("/api/machines/trust", { id: m.id, fingerprint: seen.fingerprint });
      $("#machines-dialog").close("yes");
    } catch (err) {
      problem.replaceChildren(notice("error", err.message));
      buttons.forEach((b) => { b.disabled = false; });
    }
  });
  if ((await showDialog(form)) === "yes") {
    toast(`Pinned ${m.name}'s key`, "Connect is ready.");
    await load();
  }
}

async function keyChanged(m, seen) {
  const yes = await confirmDialog(`${m.name}'s key has changed`,
    [el("p", {}, `Pinned: ${seen.pinned.join(", ")}`, el("br"), `It shows now: ${seen.fingerprint}`),
      el("p", {}, "This is what someone listening in between would look like. It's also what a reinstalled server, "
        + "or one given a new key, looks like. Ixel won't connect to it until you forget the old key: do that only if "
        + "you know why it changed.")],
    "Forget the old key", true);
  if (!yes) return;
  try {
    const done = await postJSON("/api/machines/forget", { id: m.id });
    toast("Forgot the old key", done.message);
  } catch (e) {
    toast("Couldn't forget the key", e.message, "error");
  }
  await load();
}

async function bringIn(source) {
  let done;
  try {
    done = await postJSON("/api/machines/import", { from: source });
  } catch (e) {
    toast("Couldn't import", e.message, "error");
    return;
  }
  await load();  // the list shows them behind the message
  await infoDialog(done.added ? "Imported" : "Nothing new", done.message);
}

async function importMenu() {
  const im = data.imports;
  const options = [];
  if (im.ssh_config) options.push(["ssh_config", `${plural(im.ssh_config, "host")} from ~/.ssh/config`,
    "ssh keeps reading each one's address, user, port, key and jump host from that file."]);
  if (im.console && im.console.machines) options.push(["console", `${plural(im.console.machines, "machine")} from Ixel Console`,
    "With the keys it pinned. Its gateway chat profiles stay behind."]);
  const form = el("form", { method: "dialog" }, el("h2", {}, "Import machines"),
    el("div", { class: "import-options" }, options.map(([value, label, hint]) =>
      el("button", { type: "submit", class: "import-option", value },
        el("b", {}, label), el("small", {}, hint)))),
    el("div", { class: "dialog-buttons" },
      el("button", { type: "button", class: "btn", onclick: () => $("#machines-dialog").close("") }, "Cancel")));
  const chosen = await showDialog(form);
  if (chosen) await bringIn(chosen);
}

// ── Adding and editing ────────────────────────────────────────────────────

async function editMachine(m) {
  const fresh = !m;
  const agents = data ? data.agents : {};
  const value = m || { name: "", host: "", user: "", port: null, key: "", agent: "shell", command: "", group: "", notes: "" };
  const input = (id, attrs, v) => {
    const node = el("input", { type: "text", id, autocomplete: "off", spellcheck: "false", ...attrs });
    node.value = v === null || v === undefined ? "" : String(v);
    return node;
  };
  const name = input("m-name", { maxlength: "80", placeholder: "Like Home lab, or Hetzner" }, value.name);
  const host = input("m-host", { maxlength: "253", required: true, placeholder: "example.com, 203.0.113.7, or a Host from ~/.ssh/config", class: "mono" }, value.host);
  const user = input("m-user", { maxlength: "128", placeholder: "Your login name", class: "mono" }, value.user);
  const port = input("m-port", { inputmode: "numeric", maxlength: "5", placeholder: "22", class: "mono" }, value.port);
  const key = input("m-key", { maxlength: "1024", placeholder: "~/.ssh/id_ed25519", class: "mono" }, value.key);
  const group = input("m-group", { maxlength: "60", placeholder: "Optional", list: "m-groups" }, value.group);
  const groupsList = el("datalist", { id: "m-groups" },
    [...new Set((data ? data.machines : []).map((x) => x.group).filter(Boolean))].map((g) => el("option", { value: g })));
  const notes = el("textarea", { id: "m-notes", rows: "2", maxlength: "2000" });
  notes.value = value.notes || "";
  const agent = el("select", { id: "m-agent" },
    Object.entries(agents).map(([key2, a]) => el("option", { value: key2 }, a.label)));
  agent.value = value.agent;
  const preset = el("select", { id: "m-preset", "aria-label": "Command" });
  const custom = input("m-command", { maxlength: "1024", placeholder: "Like tmux attach, or htop", class: "mono", "aria-label": "Command" }, value.agent === "custom" ? value.command : "");
  const showCommand = () => {
    const a = agents[agent.value] || { commands: [] };
    const presets = a.commands.filter(Boolean);
    preset.hidden = agent.value === "custom" || !presets.length;
    custom.hidden = agent.value !== "custom";
    const keep = presets.includes(preset.value) ? preset.value : presets.includes(value.command) ? value.command : presets[0];
    preset.replaceChildren(...presets.map((c) => el("option", { value: c }, c)));
    if (keep) preset.value = keep;
  };
  agent.addEventListener("change", showCommand);
  showCommand();

  const field = (id, label, control, hint) => el("div", { class: "field" }, el("label", { for: id }, label), control,
    hint ? el("small", {}, hint) : null);
  const problem = el("div", { class: "dialog-problem", role: "alert" });
  const newKey = el("button", { type: "button", class: "btn" }, "Make a new key");
  newKey.addEventListener("click", () => busy(newKey, async () => {
    try {
      const made = await postJSON("/api/machines/new-key", {});
      key.value = made.path;
      problem.replaceChildren(el("div", { class: "board-notice" }, icon("check"), el("span", {}, made.message)));
    } catch (e) {
      problem.replaceChildren(notice("error", e.message));
    }
  }));

  const extra = [];
  if (m) {
    const pinned = m.pin.state === "pinned";
    extra.push(el("div", { class: "field machine-key-state" },
      el("label", {}, "Its key"),
      el("div", { class: "machine-key-row" },
        el("span", { class: "mono" }, pinned ? m.pin.fingerprints.join(", ") : "Not checked yet"),
        el("button", { type: "button", class: "btn", onclick: () => { $("#machines-dialog").close("check"); checkKey(m, null); } },
          pinned ? "Check it again" : "Check it"),
        pinned ? el("button", { type: "button", class: "btn", onclick: (e) => forgetKey(m, e.currentTarget, problem) }, "Forget it") : null)));
    if (m.key) {
      extra.push(el("div", { class: "field" },
        el("label", {}, "Signing in with your key"),
        el("div", { class: "machine-key-row" },
          el("small", {}, "Puts your public key on the server, once. A terminal opens and asks your password there."),
          el("button", { type: "button", class: "btn", onclick: (e) => copyKey(m, e.currentTarget, problem) }, "Copy my key to it"))));
    }
  }

  const del = m ? el("button", { type: "button", class: "btn danger" }, "Delete") : null;
  const form = el("form", { method: "dialog", class: "machine-form" },
    el("h2", {}, fresh ? "Add a machine" : `Edit ${m.name}`),
    field("m-host", "Address", host, "Its name or IP address, or a Host name from your ~/.ssh/config (then ssh uses that Host's settings)."),
    el("div", { class: "field-row" },
      field("m-user", "User", user),
      field("m-port", "Port", port)),
    field("m-name", "Name", name),
    el("div", { class: "field" }, el("label", { for: "m-key" }, "Key file"),
      el("div", { class: "machine-key-row" }, key, newKey),
      el("small", {}, "Empty: ssh uses your usual keys and ssh-agent.")),
    el("div", { class: "field" }, el("label", { for: "m-agent" }, "When you connect, run"),
      el("div", { class: "machine-key-row" }, agent, preset, custom)),
    field("m-group", "Group", group),
    groupsList,
    field("m-notes", "Notes", notes),
    ...extra,
    problem,
    el("div", { class: "dialog-buttons" }, del, el("span", { class: "spacer" }),
      el("button", { type: "button", class: "btn", onclick: () => $("#machines-dialog").close("cancel") }, "Cancel"),
      el("button", { type: "submit", class: "btn primary", value: "yes" }, fresh ? "Add it" : "Save")));

  if (del) {
    del.addEventListener("click", async () => {
      if (!(await inlineConfirm(del, "Delete it?"))) return;
      try {
        await postJSON("/api/machines/delete", { id: m.id });
        $("#machines-dialog").close("deleted");
      } catch (e) {
        problem.replaceChildren(notice("error", e.message));
      }
    });
  }
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const buttons = [...form.querySelectorAll("button")];
    if (buttons.some((b) => b.disabled)) return;
    const command = agent.value === "custom" ? custom.value.trim() : agent.value === "shell" ? "" : preset.value;
    const machine = {
      name: name.value.trim(), host: host.value.trim(), user: user.value.trim(), port: port.value.trim() || null,
      key: key.value.trim(), agent: agent.value, command, group: group.value.trim(), notes: notes.value,
    };
    if (m) machine.id = m.id;
    buttons.forEach((b) => { b.disabled = true; });
    problem.replaceChildren();
    try {
      await postJSON("/api/machines/save", { machine });
      $("#machines-dialog").close("yes");
    } catch (err) {
      problem.replaceChildren(notice("error", err.message));
      buttons.forEach((b) => { b.disabled = false; });
    }
  });
  const answer = showDialog(form);
  (fresh ? host : name).focus();
  const result = await answer;
  if (result === "yes" || result === "deleted") await load();
}

async function inlineConfirm(button, words) {
  // A second press within a few seconds confirms (the dialog is already open)
  if (button.dataset.armed === "1") return true;
  const before = button.textContent;
  button.dataset.armed = "1";
  button.textContent = words;
  setTimeout(() => { button.dataset.armed = ""; button.textContent = before; }, 4000);
  return false;
}

async function forgetKey(m, button, problem) {
  if (!(await inlineConfirm(button, "Forget it? Press again"))) return;
  await busy(button, async () => {
    try {
      const done = await postJSON("/api/machines/forget", { id: m.id });
      problem.replaceChildren(el("div", { class: "board-notice" }, icon("check"), el("span", {}, done.message)));
      button.remove();
      load();
    } catch (e) {
      problem.replaceChildren(notice("error", e.message));
    }
  });
}

async function copyKey(m, button, problem) {
  await busy(button, async () => {
    try {
      const done = await postJSON("/api/machines/copy-key", { id: m.id });
      problem.replaceChildren(el("div", { class: "board-notice" }, icon("check"),
        el("span", {}, `Opened ${done.terminal}: sign in there with your password, once.`)));
    } catch (e) {
      problem.replaceChildren(notice("error", e.message));
    }
  });
}

// ── Running a command on several machines ────────────────────────────────

function focusRun() {
  const box = $("#run-command");
  if (box) box.focus();
}

function runForm() {
  const pinned = data.machines.filter((m) => m.pin.state === "pinned");
  if (runDraft.ids === null) runDraft.ids = pinned.map((m) => m.id);
  const command = el("input", { type: "text", id: "run-command", class: "mono", maxlength: "2000", autocomplete: "off",
    spellcheck: "false", placeholder: "Like uptime, df -h, or openclaw status", required: true });
  command.value = runDraft.command;
  command.addEventListener("input", () => { runDraft.command = command.value; });
  const timeout = el("select", { id: "run-timeout", "aria-label": "Time limit" },
    data.timeouts.map((t) => el("option", { value: String(t) }, t >= 120 ? `${t / 60} min each` : `${t} s each`)));
  timeout.value = String(runDraft.timeout);
  timeout.addEventListener("change", () => { runDraft.timeout = Number(timeout.value); });
  const checkboxes = [];
  const goWords = el("span", {}, "Run");
  const count = () => {
    const n = checkboxes.filter((b) => b.checked).length;
    goWords.textContent = n ? `Run on ${plural(n, "machine")}` : "Run";
  };
  const boxes = data.machines.map((m) => {
    const ok = m.pin.state === "pinned";
    const box = el("input", { type: "checkbox", value: m.id, disabled: !ok });
    box.checked = ok && runDraft.ids.includes(m.id);
    box.addEventListener("change", () => { runDraft.ids = checkboxes.filter((b) => b.checked).map((b) => b.value); count(); });
    checkboxes.push(box);
    return el("label", { class: `run-pick${ok ? "" : " off"}`, title: ok ? "" : "Check its key first" }, box,
      el("span", {}, m.name), ok ? null : el("small", {}, "key not checked"));
  });
  count();
  const go = el("button", { type: "submit", class: "btn primary" }, icon("play"), goWords);
  const running = run && run.running;
  go.disabled = Boolean(running);
  const form = el("form", { class: "run-form", "aria-label": "Run on machines" },
    el("div", { class: "run-line" },
      el("label", { for: "run-command", class: "sr-only" }, "Command"), command, timeout, go),
    el("div", { class: "run-picks" }, boxes),
    el("p", { class: "fine" }, "Runs on up to 8 at once, with nothing asked: a key or ssh-agent signs in (a password "
      + "can't). Output past 64 KB per machine isn't kept."));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const ids = checkboxes.filter((b) => b.checked).map((b) => b.value);
    if (!command.value.trim()) { command.focus(); return; }
    if (!ids.length) { toast("Pick the machines to run it on", "", "error"); return; }
    await busy(go, async () => {
      try {
        run = await postJSON("/api/machines/run", { command: command.value, timeout: Number(timeout.value), ids });
        expanded.clear();
        render();
        poll();
      } catch (err) {
        toast("Couldn't run it", err.message, "error");
      }
    });
  });
  return form;
}

function runResults() {
  const counts = run.counts || {};
  const done = ["ok", "failed", "error", "timeout", "stopped"].reduce((n, s) => n + (counts[s] || 0), 0);
  const summary = run.running
    ? `Running on ${plural(run.results.length, "machine")}: ${done} done`
    : [counts.ok ? `${counts.ok} done` : "", counts.failed ? `${counts.failed} failed` : "",
      counts.error ? `${counts.error} couldn't run` : "", counts.timeout ? `${counts.timeout} timed out` : "",
      counts.stopped ? `${counts.stopped} stopped` : ""].filter(Boolean).join(", ");
  return el("section", { class: "run-results", "aria-label": "The last run" },
    el("div", { class: "run-head", "data-sig": JSON.stringify([run.id, run.running, summary]) },
      el("code", { class: "run-command" }, run.command),
      el("span", { class: "page-sub", role: "status" }, summary),
      el("span", { class: "spacer" }),
      run.running
        ? el("button", { type: "button", class: "btn danger", onclick: (e) => stopRun(e.currentTarget) }, icon("stop"), el("span", {}, "Stop"))
        : el("button", { type: "button", class: "icon-btn", "aria-label": "Close the results", title: "Close", onclick: () => { run = null; render(); } }, icon("x"))),
    el("ul", { class: "run-list" }, run.results.map(resultRow)));
}

function resultRow(r) {
  const open = expanded.has(r.id);
  const toggle = el("button", { type: "button", class: "run-row", "aria-expanded": String(open) },
    el("span", { class: `run-state s-${r.state}`, title: STATE_WORDS[r.state] }, icon(STATE_ICONS[r.state] || "circle")),
    el("span", { class: "run-name" }, r.name),
    el("span", { class: "run-preview mono" }, r.hint && r.state !== "ok" ? r.hint : r.preview),
    el("span", { class: "run-meta" }, [STATE_WORDS[r.state], r.seconds !== null && r.seconds !== undefined ? `${r.seconds}s` : ""].filter(Boolean).join(" · ")));
  toggle.addEventListener("click", () => {
    if (open) expanded.delete(r.id); else expanded.add(r.id);
    refreshRun();
  });
  const machine = data && data.machines.find((x) => x.id === r.id);
  return el("li", { class: `run-item s-${r.state}`, "data-id": r.id, "data-sig": JSON.stringify([open, r]) }, toggle,
    open ? el("div", { class: "run-output" },
      r.hint ? el("p", { class: "run-hint" }, r.hint) : null,
      r.hint_code === "hostkey" && machine
        ? el("button", { type: "button", class: "btn", onclick: (e) => checkKey(machine, e.currentTarget) }, icon("lock"), el("span", {}, "Check its key"))
        : null,
      r.output !== undefined
        ? el("pre", {}, r.output || "(no output)", r.cut ? `\n… ${plural(r.bytes, "byte")} in all; Ixel keeps the first 64 KB.` : "")
        : el("span", { class: "spin" }),
      r.output ? copyButton(() => r.output, "Copy the output") : null) : null);
}

async function refreshRun() {
  if (!run) return;
  try {
    run = await getJSON(`/api/machines/run?id=${encodeURIComponent(run.id)}&show=${encodeURIComponent([...expanded].join(","))}`);
  } catch (e) {
    if (e.status === 404) run = null;
  }
  patchRun();
}

// Only what changed in the run is redrawn: what you're typing, the rows, and where you scrolled in an
// output stay as they are
function patchRun() {
  const section = $("#machines-body .run-results");
  if (!section || !run) { render(); return; }
  const fresh = runResults();
  const list = section.querySelector(".run-list");
  const items = [...fresh.querySelectorAll(".run-item")];
  const before = [...list.children];
  if (items.length !== before.length || items.some((li, i) => before[i].dataset.id !== li.dataset.id)) {
    section.replaceWith(fresh);  // another run
  } else {
    const head = section.querySelector(".run-head");
    const freshHead = fresh.querySelector(".run-head");
    if (head.dataset.sig !== freshHead.dataset.sig) head.replaceWith(freshHead);  // Stop keeps its focus
    items.forEach((li, i) => {
      const old = before[i];
      if (old.dataset.sig === li.dataset.sig) return;
      const pre = old.querySelector("pre");
      const top = pre ? pre.scrollTop : 0;
      const focused = old.contains(document.activeElement);
      old.replaceWith(li);
      const now = li.querySelector("pre");
      if (now) now.scrollTop = top;
      if (focused) li.querySelector(".run-row").focus();
    });
  }
  const go = $("#machines-body .run-form button[type=submit]");
  if (go) go.disabled = Boolean(run.running);
}

function poll() {
  stopPolling();
  polling = setInterval(async () => {
    if ($("#view-machines").hidden || !run) { stopPolling(); return; }
    await refreshRun();
    if (run && !run.running) stopPolling();
  }, 800);
}

function stopPolling() {
  if (polling) clearInterval(polling);
  polling = null;
}

async function stopRun(button) {
  await busy(button, async () => {
    try {
      run = await postJSON("/api/machines/run/stop", { id: run.id });
    } catch (e) {
      toast("Couldn't stop it", e.message, "error");
    }
    await refreshRun();
  });
}

// ── Dialogs ───────────────────────────────────────────────────────────────

function showDialog(...children) {
  const dialog = $("#machines-dialog");
  dialog.replaceChildren(...children);
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue), { once: true }));
}

function dialogButtons(yes, danger) {
  return el("div", { class: "dialog-buttons" },
    el("button", { type: "button", class: "btn", onclick: () => $("#machines-dialog").close("cancel") }, "Cancel"),
    el("button", { type: "submit", class: `btn ${danger ? "danger-fill" : "primary"}`, value: "yes" }, yes));
}

async function confirmDialog(title, body, yes, danger) {
  const answer = showDialog(el("form", { method: "dialog" }, el("h2", {}, title), ...body, dialogButtons(yes, danger)));
  $("#machines-dialog .dialog-buttons .btn:first-child").focus();  // the safe choice
  return (await answer) === "yes";
}

async function infoDialog(title, text, line) {
  const answer = showDialog(el("form", { method: "dialog" }, el("h2", {}, title), el("p", {}, text),
    line ? el("div", { class: "check-fix" }, el("code", {}, line), copyButton(() => line, "Copy")) : null,
    el("div", { class: "dialog-buttons" }, el("button", { type: "submit", class: "btn primary", value: "ok" }, "OK"))));
  $("#machines-dialog .dialog-buttons .btn").focus();
  await answer;
}
