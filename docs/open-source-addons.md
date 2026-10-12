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

Add-ons are now described by cards; the format, state and Python API are in [addon-cards.md](addon-cards.md).

Files: `ixel_mat/addons.py` (install/remove/locate, prompt building, HTML extraction, self-check),
routes in `ixel_mat/gui/settings_api.py` / `server.py`, UI in `settings.js` and `ask.js`.
Tests in `tests/test_addons.py`.

### Settings → Add-ons
- New section **Add-ons**, one row: "Diagram Design: diagrams as one HTML file, in your brand. By
  Cathryn Lavery, MIT." with a switch, off by default.
- On: download `https://codeload.github.com/cathrynlavery/diagram-design/tar.gz/<PINNED_COMMIT>`,
  extract only `skills/diagram-design/` into a temporary folder (refuse absolute paths, `..`, links and
  devices), check the extracted files against the tree hash pinned in its card, and only then move
  them to `~/.ixel/addons/diagram-design/files/`; a mismatch is refused. The tree hash, not the tarball's
  sha256, because codeload's gzip output isn't guaranteed byte-stable: for each regular file under
  `skills/diagram-design/`, take (its path relative to that folder with `/`, the sha256 of its bytes),
  sort by path, join as `f"{rel}\0{sha}\n"` and sha256 the UTF-8 result. Pinned: commit `7a2e221`,
  279 files, `e4adecc6…bfbb4a1`. Turning it
  on shows the approval dialog first; on/off and approval are kept in `~/.ixel/addons/state.json`. Off: removes
  `files/`. An install made by an earlier Ixel (files straight in `~/.ixel/addons/diagram-design/`, and
  `diagram_design = true` under `[addons]` in config.toml) isn't carried over: turn the switch on again to
  approve it, and the old files and the old line are removed then.
- `GET /api/addons` lists add-ons with on/off, version and folder path (the agent can load the skill
  from there).

### Ask → Diagram
- With the add-on on, the composer shows a **Diagram** toggle; typing `/diagram ` at the start of a
  question turns it on.
- With it on, the question goes to one model (picked beside the Diagram toggle: by default Haiku when a ready panel member can run it, else the cheapest
  ready model by price) instead of the panel.
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

### Build notes
- The diagram is shown from `GET /diagram/<id>` in a sandboxed iframe (`sandbox="allow-scripts"`, its
  own CSP, no same-origin) rather than via `srcdoc`, because a `srcdoc` frame inherits the page's CSP
  and would block the diagram's own scripts.
- The release is pinned in the card, `ixel_mat/addons/diagram-design.toml` (`[source]` `commit`,
  `tree_sha256` and `files`). To move the pin: download the new commit, compute `addons.tree_hash()` of its
  `skills/diagram-design/` folder, and update all three. Anyone who had it on approves it again, since the card
  changed.
- Diagram calls to a local model (Ollama, LM Studio) wait up to 600 seconds instead of 180, and a failure says
  the model needs a context window of at least 16k tokens for the skill's material.

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
Run on 2026-10-12 on a Mac mini (Apple M4, 16 GB, internal SSD), AirLLM 4.0.0 with MLX 0.32.3, Python
3.13. Same prompt everywhere ("Explain in two sentences why the sky is blue."), greedy, 50 new tokens.

| Run | Weights | Seconds per token | 50 tokens | Peak memory |
|---|---|---|---|---|
| AirLLM, TinyLlama 1.1B Chat (Llama layout) | fp16, split per layer (2.0 GB) | **1.99** | 99 s (+30 s to split the layers once) | 1.66 GB (process) |
| Ollama, `tinyllama` | Q4 (0.7 GB) | 0.008 | 0.4 s (+4.9 s load) | 0.7 GB (model) |
| Ollama, `qwen3:4b-instruct` | Q4 (2.5 GB) | 0.029 | 1.5 s | 2.9 GB (model) |
| AirLLM, Qwen3-0.6B | fp16 | **fails** | | 1.94 GB before the error |

- TinyLlama runs and answers sensibly, but about 250 times slower than Ollama with the same model, and
  ~70 times slower than a 4B model in Ollama. Every token reloads every layer from disk.
- Qwen3-0.6B splits fine, then stops with `ValueError: Module does not have parameter named "q_norm"`:
  `AutoModel` on macOS always builds the MLX Llama class, so a non-Llama model can't run on a Mac at
  all, whatever its size. The headline models (Qwen3, DeepSeek, Kimi) are in that group.
- Scaling the TinyLlama number by size on disk, a 70B Llama would take roughly 30 s per token at 4-bit
  (~35 GB) and about 2 minutes at fp16 (~140 GB) here.

**Verdict:** no AirLLM integration. On a 16 GB Mac, Ollama (or LM Studio) with a quantised model is two
orders of magnitude faster for anything that fits, and AirLLM's big-model path doesn't run on macOS.
Downloaded weights and split-layer folders were deleted after the test.

---

## How this gets built

On the Ixel Board on the Mac, as Handoff tasks. A planner at the top writes the plan; cheaper models
build the pieces; the planner costs the most and everything under it should be cheap.
