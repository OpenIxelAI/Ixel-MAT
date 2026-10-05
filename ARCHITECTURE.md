# Architecture

Ixel MAT has one engine: a panel of models answers a question, the models review each other anonymously,
and one of them writes the verdict. Three front ends drive it: the terminal, the browser app and the
MCP plugin. The engine doesn't know which one is calling.

```
            ┌───────────── front ends ──────────────┐
  terminal  │  mat.py (REPL)   cli.py (ixel review) │
  browser   │  gui/server.py + gui/static/*          │──► runtime.py ──► modes/review.py ──► agents/*
  plugin    │  mcp_server.py                         │    settings,       the rounds         one transport
            └────────────────────────────────────────┘    connect panel                      per model
                         │                                                    │
                         └── review_ui.py (Rich rendering)     stats.py ◄─────┘ (cost; saver runs → saves)
                             tui.py, prompt_ui.py, theme.py: the terminal's look and prompt
```

## Package layout

| Path | What it does |
|---|---|
| `ixel_mat/cli.py` | `ixel …` commands: setup, review, gui, mcp, saves, status, model, config, agents, doctor, update, machines |
| `ixel_mat/mat.py` | The interactive terminal: a plain question (`[review] plain_questions`), `/review`, `/saver`, `/answers`, `/saves`, `/compare`; ctrl+c cancels a running review (`_interruptible`) |
| `ixel_mat/prompt_ui.py` | The prompt: history, completion, multi-line input, big pastes folded into a placeholder, the status rule above it (`prompt_toolkit`, imported only when there's a terminal; otherwise `mat.py` reads plain lines) |
| `ixel_mat/asking.py` | Questions asked in the terminal (`ixel setup`, and `mat.py`'s plain lines): Rich's `Prompt` and `Confirm`, with the answer typed into `prompt_toolkit`, which draws the same prompt and keeps backspace and the arrow keys inside the answer (pipes, hidden answers and Windows go Rich's own way) |
| `ixel_mat/tui.py` | What the prompt and welcome card show, as plain functions (tested without a terminal): the logo-and-details card, what completes (`complete`), the rule above the prompt (`rule_fragments`) |
| `ixel_mat/theme.py` | The palette (the moon logo's colors) and glyphs every terminal screen draws from |
| `ixel_mat/commands.py` | Command registry shared by both (help text, prefix matching) |
| `ixel_mat/runtime.py` | Loads config, parses `[review]` and `[saver]`, connects the panel in parallel |
| `ixel_mat/modes/review.py` | The review engine: rounds, prompts, scoring, saver mode |
| `ixel_mat/modes/full.py` | The side-by-side mode (`/compare`) |
| `ixel_mat/review_ui.py` | Rich rendering: the live progress view (a finished round is printed into the terminal's history, so only the running one is live), answer panels, scoreboard, verdict, saves |
| `ixel_mat/stats.py` | Saver-mode saves counter, streaks, achievements, how the big model's first look went, and what reviews cost by month (`~/.config/ixel-mat/stats.json`) |
| `ixel_mat/usage.py` | Token counts each transport reports (`Usage`), how each agent is billed, Claude's prices and `[pricing]`, a run's cost (`CallUsage`, `totals`) and saver mode's saving |
| `ixel_mat/material.py` | Code for the panel to review: a git diff or chosen files, read once and read-only, with the secret, size and hidden-character checks |
| `ixel_mat/pictures.py` | Pictures attached in the app: PNG or JPEG only, metadata taken out (`read_picture`), kept in memory for 30 minutes (`PictureStore`) |
| `ixel_mat/sound.py` | Sound recorded or attached in the app, written out by OpenAI or Groq (`pick_provider`, `transcribe`); never kept |
| `ixel_mat/triage.py` | Optional triage (your own model or TypeSafe's decision API): auto mode, skipping agreed reviews, saver's gate |
| `ixel_mat/gui/` | `ixel gui`: an aiohttp server on 127.0.0.1 and a dependency-free HTML/JS/CSS app; `window.py` is `ixel app`, which shows it in an Edge or Chrome app window, or the native windows: `macos/` (Ixel.app, Swift) and `linux_window.py` (GTK), which run `ixel app --host` |
| `ixel_mat/machines/` | Machines (SSH): `store.py` (`machines.json`, and imports from `~/.ssh/config` and Ixel Console), `ssh.py` (pinned host keys, learned with ssh into a temporary file; the ssh lines, with the destination after `--`), `terminal.py` (Connect's terminal window, given the argv unjoined, held open by a Python helper until Enter), `runs.py` ("Run on machines": 8 at a time, time and output limits), `log.py` (`machines.log`), `cli.py` (`ixel machines`) |
| `ixel_mat/mcp_server.py` | `ixel mcp`: the MCP server (tools `ixel_review`, `ixel_panel`) and host setup snippets |
| `ixel_mat/agents/` | Transports, one per kind of model (below); `launch.py` finds the program to run for a CLI (PATH only, and npm `.cmd` shims resolved on Windows) |
| `ixel_mat/config/` | `loader.py` (TOML → `AgentConfig`, `set_agent_model`), `secrets.py` (.env, child environments, file I/O), `setup.py` (the wizard) |
| `ixel_mat/presets.py` | The locked-down subscription CLI presets; configs refer to them by name (`preset = "codex"`) |
| `ixel_mat/models.py` | `latest` / `latest-fast`: which model id each means, from a provider's live list |
| `ixel_mat/sanitize.py` | Strips terminal control sequences; escapes Rich markup |
| `ixel_mat/conversation.py` | The saved conversation `ixel review --continue` picks up (last three exchanges, 0600) |
| `ixel_mat/update.py` | `ixel update` and the once-a-day update notice. Installer copies record where they came from in `install.json`, and, as the installer's last step, the commit installed (git pull, then the installer again whenever the checkout isn't that commit, unless install.ps1 is still running in its window: it holds `install.lock` open until it's done). pipx and uv copies are recognized from pip's `direct_url.json` plus `pipx_metadata.json` / `uv-receipt.toml`, compared with `git ls-remote`, and updated by that tool |

## Agents

Every model is a `BaseAgent` with `connect()`, `send_and_receive(message, **kwargs)` and `disconnect()`.
`AgentConfig` (in `agents/base.py`) holds everything a config file can set.

| `type` | Class | Used for |
|---|---|---|
| `http` | `HttpAgent` | Anthropic (official SDK, with server-side model fallback for the newest models), and any OpenAI-compatible API: OpenAI, Gemini, xAI, Ollama, LM Studio |
| `oneshot` | `OneShotAgent` | A CLI run once per question: Claude Code, Codex, Gemini CLI, Copilot, OpenCode, or your own |
| `subprocess` | `SubprocessAgent` | A long-running interactive CLI (pty on POSIX, pipes on Windows) |
| `websocket` | `WebSocketAgent` | An OpenClaw gateway session (device-key auth) |

`send_and_receive(…, effort=)` sets the thinking level for one call. Saver mode uses it to run the verifier
at `verifier_effort`. HTTP agents map it to the provider's setting (Anthropic `output_config.effort`,
OpenAI-style `reasoning_effort`). CLI agents map it through `effort_args`, using the nearest level the
CLI accepts (`nearest_effort`).

`send_and_receive(…, on_usage=)` reports what a call used, as a `usage.Usage`: the Anthropic SDK's
`usage` (one per attempt in `usage.iterations`, so a declined attempt and its fallback are both priced, each
at its own model's rate), an OpenAI-style `usage` (reasoning counted outside `completion_tokens`, as xAI
does, is taken from `total_tokens`) (streamed replies ask for it with `stream_options.include_usage`, from
OpenAI and xAI only, since other servers may reject the field), or Claude Code's final `result` event
(tokens and its own `total_cost_usd`). It's called before a refusal or an error is raised, since that call
was billed too. Agents that can't tell don't call it, and the engine estimates from the text.

`send_and_receive(…, on_text=)` passes the reply on as it's written, from agents that can: HTTP agents
stream (Anthropic `messages.stream`, OpenAI-style server-sent events), and CLI agents with
`stdout_format = "claude-stream-json"` read Claude Code's `stream-json` events. Everything else ignores it
and the whole reply arrives at the end.

CLI agents run in a fresh temporary folder, with stdin closed (or carrying the prompt), the prompt after
`--` or on stdin (`prompt_via = "auto"`: on stdin once it's too long for a command line, `ARG_LIMIT`),
and an environment from `secrets.child_env()`. On Windows, `launch.resolve_argv` looks the command up on
PATH only (PATHEXT, limited to what CreateProcess can start), refuses a command that isn't there (a bare
name would be looked for in the current folder), runs a plain npm shim's target (`node.exe script.js` or
the `.exe`) instead of the `.cmd`, and refuses to pass cmd.exe syntax to any other batch file. Subprocess
agents start the same way; the updater (`git`, PowerShell, pipx/uv) uses the same PATH-only lookup (`find_on_path`). That environment drops the keys Ixel
loaded (unless `pass_env` names them) and anything in `drop_env`, adds `env`, and increments
`IXEL_PANEL_DEPTH`. `agents/process_tree.py` ends a CLI and everything it started (a process group on
POSIX, a job object on Windows). See [SECURITY.md](SECURITY.md).

## The review engine (`modes/review.py`)

`run_review(question, agents, mode=…, moderator=…, verifier=…, on_event=…)` returns a `ReviewResult`
and reports progress through `on_event(kind, data)`. The terminal, the browser (as NDJSON over HTTP)
and the MCP plugin (as progress notifications) all render the same events.

| Mode | Rounds |
|---|---|
| `quick` | answer → verdict |
| `review` | answer → review → verdict |
| `deep` | answer → review → revise → verdict |
| `saver` | answer → review → verify (→ fix → verify, if sent back) |

- **Answer:** every agent answers in parallel. Failures are recorded (`AgentFailure`) and the run continues
  with the rest. Every round's calls go through `_Run.calls`. With three or more models, once all but one
  are done, the last gets `slowest_wait` longer (`"auto"`: as long again as the others took, at least
  `SLOWEST_WAIT_MIN`) and is then left out, recorded as a failure.
- **Review:** each agent gets the other answers labeled A, B, C… in an order rotated per reviewer, and
  returns JSON: a verdict per answer (`correct`, `partially_correct`, `incorrect`, `unsure`), issues, and
  the best answer. The prompt defines each grade, so different models mean the same thing by
  `partially_correct`. `Standing` scores ignore a reviewer's grade of its own answer. A *concession* is a
  reviewer rating another answer above its own. *Agreement* measures how consistently reviewers graded
  each answer.
- **Revise** (deep): each author sees the reviews of its answer and may revise it.
- **Verdict:** the moderator (default: the author of the best-rated answer) writes the final answer from
  the answers and reviews, noting disagreements. It replies with the answer first, then a `<fence> notes`
  block of JSON (confidence, disagreements, corrections), so the answer can stream: `verdict_text` events
  carry it as it's written, never the notes. A reply in the older one-JSON-object format still parses.
  The moderator's confidence is checked against the reviews (`_check_confidence`): "high" needs an answer
  that every reviewer who graded it called correct, or it's shown as medium with a note saying why. An
  answer revised after it was graded (deep mode) doesn't count: its grades are of the earlier text.
- **Verify** (saver): the verifier gets drafts and reviews and returns
  `{"status": "confirmed", "use": "B"}`, or, when no draft is right, either
  `{"status": "send_back", "issues": […]}` (with `on_wrong = "send_back"`: the drafters fix their answers
  and the verifier checks once more) or `{"status": "corrected", "answer": …}` (with
  `on_wrong = "correct"`). With `escalate = "disagreement"`, the
  verifier is skipped when every reviewer rated every draft correct. `ReviewResult.tier_calls` counts
  cheap vs. big-model calls; `verifier_outcome` and `accepted` feed `stats.record_run`, which also counts
  whether the verifier's first look confirmed the top-rated draft, another one, or none (`_first_check`).

**Triage** (`triage.py`, optional) asks small typed questions: `make_triage()` returns a `ModelTriage` (one of
your agents replies with a little JSON, what it reads fenced like any model output) or a `TypeSafeTriage`
(TypeSafe's decision API returns probabilities).
Front ends call `runtime.choose_mode()` first: for `"auto"` it asks how much checking the question needs,
and picks quick, review or deep (the configured mode if triage can't answer). The engine can ask whether
answers agree: after the answer round with `[triage] skip_review` (sure enough: skip review and revise),
and before the verify round in saver mode with `escalate = "disagreement"` (the verifier is skipped only if
triage is sure the drafts agree and no reviewer graded one incorrect or partially correct). Each decision
is a `TriageDecision`: emitted as a `triage` event, kept in `ReviewResult.triage`, counted in
`triage_calls`. A failed call only means the usual rounds run.

**Follow-ups:** `run_review(…, earlier=[EarlierTurn(question, answer), …])` puts the last three questions
and final answers, fenced, before the question in every round's prompt.

**Code to review:** `run_review(…, material=Material(title, text, files))` puts the code, fenced as
`material`, after any earlier turns and before the question, in every round's prompt, with hidden
characters marked (`mark_hidden_characters`). It's kept apart from the question, so follow-ups and triage
only see the question; `ReviewResult.material` records its title, files and size. `material.py` builds it
(`git_diff`, `read_files`, `pasted`) and `check()` refuses empty, oversized (`MAX_MATERIAL_CHARS`) or
secret-looking material before any model is called. Every front end goes through `code_for_review()`, which
does both and fills in the default question when there's code and no question.

**Pictures:** `run_review(…, pictures=[Picture])` gives them to every call to an agent whose
`AgentConfig.sees_pictures` (HTTP agents only: `accepts`, else the four big APIs); the others' prompts start
by saying there are pictures they can't see. `HttpAgent` sends them as OpenAI `image_url` parts or Anthropic
image blocks, and when the API refuses them (400, 413, 415, 422) asks again without them, for the rest of
the run. The app uploads each one to `POST /api/pictures` (its own body, read with a counted 3.9 MB cap; every
other route keeps 512 KB) and names their ids in `/api/review`.

**Sound:** no model hears sound. The app posts it to `POST /api/sound` (application/octet-stream, read with a
counted 25 MB cap), which checks it's sound (`sound_kind`: WebM, Ogg, WAV, FLAC, M4A, MP3), sends it to the
service `sound.pick_provider` chooses (`[sound] provider` and only that one, else the first of OpenAI and
Groq with a key) as a multipart upload, and returns the text; the page puts it into the question. `/api/panel`
says which service that is (`sound`: its name and label, or the problem when there's none), so the page can
say where sound goes before it's sent, and the page names it in `?expect=`: when the settings changed since,
nothing is sent (409 `sound_service_changed`).

**Video:** the server never sees one. `video.js` in the page takes frames with a `<video>` element and a
canvas (the CSP's `media-src blob:` is for that element) and attaches them as pictures, and takes the sound
out with `OfflineAudioContext.decodeAudioData` at 16 kHz, made mono WAV in equal pieces of at most 10
minutes that each go to `/api/sound`. That decodes the whole file at once, so a video over 21 minutes, or
one whose length the browser can't tell and is over 25 MB, isn't decoded at all, and one decode runs at a
time. A part with no words in it (`code: "no_words"`) doesn't stop the others, and Send it again carries on
from the part that failed. A WebM whose tracks are all sound (`soundOnly`; browsers call every .webm a
video) is sent as it is, like any sound file; its list of tracks is parsed, and has to be whole in the file's
first megabyte, or it's taken for a video, so a video is never sent whole by mistake.

**Cost:** `_Run.ask` hands every call an `on_usage` and records a `CallUsage` for it (as reported, or
estimated at four characters a token) with its billing (`usage.billing_for`: api, plan, local, unknown)
and, for API keys, its price (`price_for`: `[pricing]` first, then Claude's list). A failed call is
recorded only if it reported what it used. Triage's calls, when one of your models answers them, come back
on each `TriageDecision.usage` and are added to the run's. After a saver run the verifier confirmed (or
skipped),
`saver_saving` estimates the answer it didn't write. `ReviewResult.to_dict()` carries both, with the
one-line summaries the front ends show, and `stats.record_run` adds every review's API spend (and saver's
saving) to the month's totals. The terminal keeps its conversation
in memory (`/new` clears it), the browser page sends it with each request, and `ixel review --continue`
loads it from `conversation.py`.

All text a model wrote is fenced with a random per-run marker before it reaches another model, and
sanitized so the marker can't be forged. Runs refuse to start inside another panel
(`IXEL_PANEL_DEPTH > 0`). A panel is capped at 8 models.

## Front ends

- **Terminal** (`mat.py`, `cli.py review`): `ReviewProgress` shows the running round's agents with a spinner, how many
  are back, and the time (its `echo` prints each finished round above it); `/review` and `/compare` run inside
  `mat._interruptible`, which lets ctrl+c cancel the run instead of the session;
  `report()` prints the verdict, scoreboard, concessions and flagged issues. `ixel review --json` prints
  `ReviewResult.to_dict()`, with progress on stderr.
- **Browser** (`gui/server.py`): `GET /api/panel`, `GET /api/saves`, `POST /api/review` (a streamed NDJSON
  event log, ending with the result and any saves update), and `GET /api/presence`, which each open page
  holds open so `ixel app` (`serve_window`) can stop once its window is closed. The Board's pull requests
  are `GET /api/connections` and `POST /api/connections/{host,token,review,fix}` (`gui/connections_api.py`,
  over `connections.py`, which reads the host's API without following redirects and fetches a pull request
  into `refs/ixel/pr/N/` with git's prompts off); a review is a Handoff task approved for the fetched base
  and head commits, which Handoff seals and runs as `ixel ask --agent <name> --json --base <sha> --head <sha> -`; a fix is an edit
  approved for the same commits, whose worktree branch starts from the head. Machines is `GET /api/machines`,
  `POST /api/machines/{save,delete,import,key,trust,forget,connect,copy-key,new-key}` and
  `/api/machines/run` (start, `GET` its state, `/stop`) in `gui/machines_api.py`; a key is pinned only
  with the fingerprint the page was shown from a check in the last 15 minutes, never beside a different key
  pinned for the same name (`[host]:port`, plus a hash of the jump host or ProxyCommand when there is one),
  and runs still going stop when the server does. `machines.json` and the pins file are changed under a
  lock file next to each (`store.locked`), so the app and `ixel machines` never write over each other. Static files are served from
  `gui/static/`. Every response carries the CSP and isolation headers, and a middleware checks the token,
  Host and Origin.
- **Plugin** (`mcp_server.py`): an `MCPServer` from the MCP Python SDK on stdio. `ixel_review` formats
  the result as Markdown (`format_result`), prefaced with an untrusted-content notice, and reports round
  progress. `ixel mcp --setup` prints config for Claude Desktop, Claude Code, Codex and Cursor using the
  absolute path of this install (on Windows its `python.exe -m ixel_mat mcp`, since Smart App Control can
  block pip's unsigned `ixel.exe`: with `-I` from a virtualenv, as install.ps1's `ixel.cmd` runs it, and `-P`
  outside one, which keeps user site-packages).

## Configuration

`~/.config/ixel-mat/config.toml` holds `[agents.<id>]` tables plus optional `[review]`, `[saver]`,
`[triage]`, `[sound]`, `[connections."<host>"]`, `[updates]` and `[pricing]`.
Secrets are in `~/.config/ixel-mat/.env`, loaded at start; `token_env` names the variable. The loader
validates each field and reports problems as warnings instead of failing. Config in the current folder
is never read. `config.example.toml` documents every option, and `tests/test_docs.py` keeps it loading
cleanly and matching the CLI presets.

## Tests and CI

`pytest` runs the suite. The notable pieces:

- `tests/conftest.py`: points every file Ixel keeps in `~/.config/ixel-mat`, and the installer's
  `install.json`, at each test's own folder, so the suite never writes yours; `test_user_files.py` fails if
  a module still holds a path to them.
- `tests/fake_providers.py`: an in-process fake OpenAI/Anthropic API that plays a whole panel, so reviews
  run end to end with no network.
- `tests/cli_capture.py` + `test_cli_presets_live.py`: the real subscription CLIs against a fake API that
  asks for tool calls (see SECURITY.md).
- `test_gui_browser.py`: the browser app in Chromium via Playwright, including hostile model output.
- `tests/sshd_lab.py`: a real OpenSSH server on 127.0.0.1 with its own host key, for
  `test_machines_ssh.py` and `test_gui_machines_browser.py` (skipped where there's no `sshd`).
- `test_mcp_server.py`: the plugin over real stdio, as a host app runs it.

CI (`.github/workflows/tests.yml`) runs only when started by hand (`workflow_dispatch`), not on pushes or
pull requests. It is Windows-first: a run covers pytest on Windows, `install.ps1` in Windows PowerShell 5.1
and PowerShell 7, the browser tests on Windows, and the live CLI preset job against the latest CLI
releases. It also runs pytest on Linux (Python 3.10, 3.13, 3.14), a macOS job with the installer, and `install.sh` in Debian, Fedora (plus
the suite on Fedora's Python) and Arch containers.
