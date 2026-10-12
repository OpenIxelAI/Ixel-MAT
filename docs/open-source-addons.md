# Open-source add-ons in the Ixel app

Three open-source projects were proposed for Ixel. This is where each one goes and why, and the spec
the Board builds from. All three are used as published (MIT / Apache-2.0); Ixel calls them, it does
not copy their code.

| Project | Where it goes | Why |
|---|---|---|
| [REA](https://github.com/morluto/rea) (reverse engineering, MIT) | **A tab: Reverse engineer** | It is a workflow with its own inputs (a target and a question) and a multi-step run, and the run is exactly where several models beat one: Ixel runs REA's tools for free, cheap models read the bulky evidence, and only the planner sees the short notes. |
| [Diagram Design](https://github.com/cathrynlavery/diagram-design) (diagrams as one HTML file, MIT) | **A switch in Settings, then a Diagram toggle in Ask** | It is an Agent Skill: instructions and templates, not a program. A diagram is just another kind of answer, so it belongs where you ask; a tab would be an empty box with a prompt in it. |
| [AirLLM](https://github.com/lyogavin/airllm) (layer-streaming inference, Apache-2.0) | **Nothing yet; test first** | See the AirLLM section. Build only if the test on a 16 GB Mac shows usable speed. |

---

## 1. Reverse engineer tab (REA)

Files: `ixel_mat/reverse.py` (the pipeline, no web code), `ixel_mat/gui/reverse_api.py` (routes),
`ixel_mat/gui/static/reverse.js` (the view), a rail button `data-view="reverse"` labelled
"Reverse engineer" with a new icon, and `ixel reverse TARGET "question"` in `ixel_mat/cli.py` so the
agent and the Board can run the same pipeline. Tests in `tests/test_reverse.py`.

### REA status card
- Looks for `rea` on PATH (`rea --version`). Shows the version, and which native engines REA sees
  (`rea providers --json`: Hopper, Ghidra or IDA). JavaScript/Electron and .NET need no engine;
  native binaries do, and the card says so in one line.
- Not installed: a button **Install REA** that, after a confirm dialog naming the exact command, runs
  `npm install --global rea-agents@6.4.0` (pinned; a later Ixel release moves the pin) and shows its
  output. Needs Node 22.19+/24.11+/26+; if `node` is missing or too old the card says that instead.
  Never runs `rea setup` (that edits other agents' configs).

### A run
Inputs: target (absolute local path to a file, folder, `.app` or `.asar`; must exist), the question,
a **planner** model (default: the biggest ready model), **readers** (one or more; default: the
cheapest ready ones, local and free models first, then Haiku), and how many follow-up looks
(default 6, max 12). One line under the form: "For lawful research on software you're allowed to
inspect." (REA's own disclaimer).

Pipeline, each step shown live with model, time, tokens and cost:
1. **Survey** (Ixel, $0). `rea analyze TARGET --json`, 10-minute limit, saved to
   `~/.ixel/reverse/<run-id>/survey.json`. For JavaScript evidence (often hundreds of MB), project
   the summary view with `rea inspect-analysis-view` (see REA docs/cli.md). Clip what a model sees to
   60,000 characters.
2. **Plan** (planner). Material: the survey summary. Asks for JSON only:
   `{"queries": [{"op": "...", ...}], "why": "..."}`.
3. **Gather** (Ixel, $0). Each query runs as an argv list, never through a shell. Allowed ops, all
   static and read-only:
   - native: `search {text}`, `function {name}`, `decompile {addr}`, `xrefs {addr}`,
     `instructions {addr}`, `trace {text}` → `rea <op> TARGET <arg> --json`
   - JavaScript: `page {collection, offset, limit}`, `module {id}` → `rea inspect-analysis-view`
     on the saved survey.
   Validation: unknown ops dropped; strings ≤ 200 chars, no leading `-`, no NUL/newlines; addresses
   match `^(0x)?[0-9a-fA-F]{1,16}$`; offset/limit small integers; at most the chosen number of looks.
   Runtime capture, browser, adb, process and network ops are never offered.
4. **Read** (readers, in parallel, at most 3 at a time, round-robin over the readers). Each reader gets
   ONE query result as material plus the question, and returns notes of at most 300 words: what it
   shows, the evidence (addresses, module paths, strings) and what is still unknown.
5. **Answer** (planner). Material: all the notes plus the first part of the survey summary. Writes
   how the feature works, the evidence for each claim, the unknowns, and, when asked, how to build
   something similar.

Every model call goes through `ixel_mat.ask.ask(cfg, question, material)`, so target content is
always fenced as untrusted material (strings inside a binary can say anything). Models get no tools.

Routes follow the Machines run pattern: `POST /api/reverse/run` (starts, returns id),
`GET /api/reverse/run?id=` (progress and results), `POST /api/reverse/run/stop`,
`GET /api/reverse/status` (the card), `POST /api/reverse/install`.

### Cost line
Totals per step and overall from each `AskResult.usage`, plus **"If the planner had read
everything"**: the readers' input and output tokens priced at the planner's rate
(`ixel_mat.usage.price_for` / `cost_at`), so the saving is shown, not claimed.

### Tests
A fake `rea` (a small script printing canned JSON) on PATH and fake agents: the pipeline order, argv
building, the allowlist (rejects unknown ops, leading `-`, bad addresses, too many looks), clipping,
stop, missing REA, and the cost line.

---

## 2. Diagram Design (Settings switch + Ask toggle)

Files: `ixel_mat/addons.py` (install/remove/locate, prompt building, HTML extraction, self-check),
routes in `ixel_mat/gui/settings_api.py` / `server.py`, UI in `settings.js` and `ask.js`.
Tests in `tests/test_addons.py`.

### Settings → Add-ons
- New section **Add-ons**, one row: "Diagram Design: diagrams as one HTML file, in your brand. By
  Cathryn Lavery, MIT." with a switch, off by default.
- On: download `https://codeload.github.com/cathrynlavery/diagram-design/tar.gz/<PINNED_SHA>`, check
  its sha256 against the value pinned in `addons.py`, extract only `skills/diagram-design/` into
  `~/.ixel/addons/diagram-design/` (refuse absolute paths, `..`, links and devices). Saves
  `addons.diagram_design = true`. Off: removes the folder and the setting.
- `GET /api/addons` lists add-ons with on/off, version and folder path (the agent can load the skill
  from there).

### Ask → Diagram
- With the add-on on, the composer shows a **Diagram** toggle; typing `/diagram ` at the start of a
  question turns it on.
- With it on, the question goes to one model (the first chosen on the panel) instead of the panel.
  Ixel picks the diagram type by matching the request against the type list (architecture, flowchart,
  sequence, timeline, quadrant, ...; default architecture), then sends as reference material:
  `SKILL.md`, `references/style-guide.md`, `references/type-<type>.md` and
  `assets/example-<type>.html` when it exists. The model must answer with one complete HTML document in
  a ```html block.
- Ixel extracts the HTML, saves it to `~/.ixel/diagrams/<date>-<slug>.html`, runs
  `python3 <addon>/scripts/self_check.py <file>` (standard library only) and, if it fails, retries once
  with the check's messages. Shows the diagram in a sandboxed iframe (`sandbox="allow-scripts"`, no
  `allow-same-origin`, via `srcdoc`) with **Open** and **Show in folder**, and the check result.
- Cost and tokens shown like any answer.

### Tests
Safe extraction (rejects `..`, absolute, links), sha mismatch refused, type matching, HTML extraction
from a reply, self-check pass/fail/retry with a fake model, toggle on/off through the settings route.

---

## 3. AirLLM

What it claims: run 70B models on a 4 GB GPU, and even 671B-2.8T models, by loading one layer at a
time from disk. The memory claim is real by construction; the catch is speed. Every new token reads
the whole model from disk again, so speed is roughly (model size on disk) / (disk read speed):
a 70B model at 4-bit (~35 GB) on a ~3 GB/s SSD is over 10 seconds per token at best. An agent
answer of 500 tokens would take well over an hour.

On a Mac, AirLLM's `AutoModel` always uses its MLX class, which only knows the Llama layout
(`air_llm/airllm/auto_model.py`, `airllm_llama_mlx.py`). The headline models (Qwen3, DeepSeek, Kimi)
take the generic path, which is CUDA-only. Results of the Mac test are recorded below.

### Mac test result
_(filled in by the test run)_

---

## How this gets built

On the Ixel Board on the Mac, as Handoff tasks. A planner at the top writes the plan; cheaper models
build the pieces; the planner costs the most and everything under it should be cheap.
