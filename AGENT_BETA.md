# Ixel Agent (beta)

A personal agent inside the Ixel app that works in a folder you pick, on the models you already set up in
Ixel (subscription CLIs in their answer-only presets, API keys, local models). It follows the roadmap's
"Agents that act (carefully)" plan: **the model never gets a shell or a file handle.** It asks Ixel for one
action at a time; Ixel does the reading itself, and anything that changes your computer waits for you.

It borrows from two open-source personal agents:

- **OpenClaw:** a workspace of plain files the agent reads every session (`SOUL.md` for its persona,
  `USER.md` for who you are, `MEMORY.md` for what it has learned), skills as `SKILL.md` folders, and
  approvals before anything runs on the host.
- **Hermes Agent:** agent-curated memory (it proposes what to remember), skills it can load on demand
  by name and description only (the full text is read only when used), searchable past sessions, and an
  allowlist for commands you've approved before.

What it leaves out for the beta: messaging channels, a network gateway (Ixel never listens beyond this
machine), cron jobs and sub-agents.

## How a run works

1. You pick a model, a folder (the **workspace**) and type a goal in the Agent page.
2. Ixel builds the prompt: the agent's rules and the action list, the persona/user/memory files, the
   skill index, the goal, and every step so far (each tool result fenced as untrusted data).
3. The model answers with **one JSON object**, nothing else:
   ```json
   {"thought": "short reason", "action": "read_file", "args": {"path": "src/app.py"}}
   ```
   or, when done, `{"thought": "...", "action": "finish", "args": {"answer": "what I did / found"}}`.
   A reply that isn't valid JSON is sent back once with the parse error; a second bad reply stops the run.
4. Ixel runs the action and records a **step**. Read-only actions run at once. Actions that change
   something become a **pending approval** and the run pauses until you approve or deny it in the app.
5. Repeat until `finish`, the step limit (default 25), you press Stop, or an error.

## Actions

| Action | Args | Runs |
| --- | --- | --- |
| `list_dir` | `path` (default `.`) | at once; names, kinds, sizes; at most 500 entries |
| `read_file` | `path`, optional `start`, `end` lines | at once; text only, at most 200 KB, secrets masked |
| `search` | `pattern` (plain text), optional `path`, `glob` | at once; at most 200 matching lines |
| `load_skill` | `name` | at once; the skill's `SKILL.md` text |
| `remember` | `text` | at once; appended to `MEMORY.md` with the date (shown in the app, editable) |
| `write_file` | `path`, `content` | **after your approval**; shown as a diff against the current file |
| `edit_file` | `path`, `old`, `new` | **after your approval**; `old` must appear exactly once; shown as a diff |
| `run_command` | `argv` (list of strings) | **after your approval** of the exact command, unless it's on your allowlist |
| `finish` | `answer` | ends the run |

### Rules Ixel enforces (not the model)

- Every path is resolved inside the workspace. `..`, absolute paths, and symlinks that lead outside are
  refused. So are `.git/` internals, and files that look like secrets (`.env*`, `*.pem`, `*.key`,
  `id_rsa*`, `.netrc`, Ixel's own config folder), for reading and writing alike.
- Writes are atomic (temp file then replace) and refuse binary files and files over 1 MB.
- `run_command` takes an argv list, never a shell string, runs with the workspace as its folder, a
  120-second limit, at most 64 KB of output kept, and **the environment without any keys saved in Ixel**
  (`child_env`). On macOS it runs under `sandbox-exec` with network access denied and writes allowed only
  in the workspace and the temp folder; elsewhere the approval card says it is not sandboxed.
- The allowlist holds exact argv prefixes you approved with "Always allow" (for example `["pytest"]`),
  per workspace. It never matches shells or interpreters with `-c` (`sh`, `bash`, `zsh`, `python -c`,
  `node -e`), which always ask.
- Everything a tool returns goes into the prompt inside a random fence, with the note that it's data,
  not instructions (the same fencing `ixel ask` uses for material).

## Files

Under `~/.config/ixel-mat/agent/` (Windows: the same folder Ixel's config uses):

- `SOUL.md`, `USER.md`, `MEMORY.md`: created with short defaults on first use; editable from the app.
  Each is clipped to 16 KB in the prompt.
- `skills/<name>/SKILL.md`: front matter with `name:` and `description:` (the AgentSkills format).
  Only names and descriptions go into the prompt.
- `sessions/<id>.json`: each run's goal, model, workspace, steps, approvals and usage.
- `allow.json`: the command allowlist, by workspace.

## Code

- `ixel_mat/agent/` — `protocol.py` (the prompt and parsing a reply), `tools.py` (workspace paths and
  the actions), `store.py` (memory, skills, sessions, allowlist), `runner.py` (the loop, pausing for
  approvals, stopping), `sandbox.py` (running an approved command).
- `ixel_mat/gui/agent_api.py` and routes under `/api/agent/…` in `server.py`.
- `ixel_mat/gui/static/agent.js`, an **Agent** item on the rail with a **Beta** badge, styles in
  `style.css`.
- Tests in new files `tests/test_agent_*.py`, including the attacks: a file that tells the agent to read
  `~/.ssh`, path traversal, symlink escapes, a secret file, a command that reaches the network.

## The app's API (the contract between the runner, the server and agent.js)

All JSON, behind the app's session token like every other route.

- `GET /api/agent/models` → `{"models": [{"name", "label", "ready"}]}`: Ixel's configured models.
- `POST /api/agent/start` `{"model", "workspace", "goal", "max_steps"?}` → `{"id"}`. The workspace must be
  an existing folder inside the home folder, and not the home folder itself.
- `GET /api/agent/run?id=…` → the run:
  ```json
  {"id": "…", "status": "running|waiting|done|stopped|failed", "goal": "…", "model": "…",
   "workspace": "…", "started": 0, "answer": "…", "error": "…",
   "steps": [{"n": 1, "thought": "…", "action": "read_file", "args": {}, "ok": true, "result": "…",
              "usage": {}}],
   "pending": {"n": 4, "action": "edit_file", "args": {}, "diff": "…", "argv": [], "sandboxed": true},
   "usage": {"calls": 3, "cost": 0.0}}
  ```
  `pending` is null unless `status` is `waiting`; `diff` is set for writes, `argv` and `sandboxed` for
  commands. Results in `steps` are clipped to 4 KB for the page (the model sees more).
- `POST /api/agent/decide` `{"id", "approve": true|false, "always": false}` → the run. `always` adds the
  command's argv to the allowlist (refused for shells and interpreters).
- `POST /api/agent/stop` `{"id"}` → the run, `stopped`.
- `GET /api/agent/runs` → `{"runs": [{"id", "goal", "status", "started", "model", "workspace"}]}`, newest first,
  at most 50.
- `GET /api/agent/files` → `{"soul", "user", "memory", "skills": [{"name", "description"}]}`.
- `PUT /api/agent/files` `{"name": "soul|user|memory", "text"}` → the same as GET.

## Usage

Each model call is recorded the way `ixel ask` records one (`call_usage`), and the run shows the total.

# Beta 2: files and pictures, computer access, a chat-style page

What changes after the first beta, from the owner's notes: the page should feel like other agent apps (simple,
one conversation, the work happening quietly), the agent must take photos, video and any other file, it gets
an opt-in **Computer access** switch with a warning, and the composer gets a `/` menu like other agents'.

## Files, photos and video (like Hermes' vision and file tools, OpenClaw's media handling)

- `read_file` also reads documents: PDF, Word, Excel, PowerPoint, RTF, ODF and the rest that
  `ixel_mat.documents.read_document` knows, returning their text (and saying how many pictures they hold).
  Plain text files work as before.
- New action `view_file` (`path`): a picture (PNG, JPEG, GIF, WebP, HEIC, BMP, TIFF) goes to the model with its
  **next** call, cleaned the way Ask cleans pictures (`ixel_mat.pictures`). A video (MP4, MOV, M4V, WebM, MKV,
  AVI) gives up to 6 frames spread over it, taken with `ffmpeg` when it's installed (each frame a picture as
  above), else its size and length only and a note that ffmpeg would show its frames. A PDF or slide deck
  gives its first pictures. A model that can't see pictures (its config's `sees_pictures` is false) is told
  so in the step's result instead, and nothing is sent. At most 8 pictures per model call, 20 MB in all.
- **Your files in a run:** the composer's attach button (and dropping files on the page) uploads them; they're
  kept in the agent's folder under `uploads/<run id>/`, never in the workspace, and the run's first prompt lists
  them. The agent reaches them as `upload:NAME` in `read_file` and `view_file`. 50 MB per file, 10 files per run.
- `list_dir` marks each entry's kind (`picture`, `video`, `document`, `text`, `other`) so the model knows
  what to use on it.

## Computer access (off by default)

A switch on the composer (and `/computer on|off`). Turning it on shows a warning once per run, which you
confirm:

> Computer access lets the agent work outside the folder you picked: read and change files anywhere in
> your home folder, run commands without the sandbox and with the network, take screenshots and open apps,
> files and links. Every change, command, screenshot and open still waits for your Approve, but an approved
> command can do anything you can. Turn it on only for a model and a goal you trust.

With it on, for that run only:
- Paths may be absolute or start with `~`, anywhere under the home folder. Secret files (the spec's list,
  plus `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/Library/Keychains`, browser profiles) are still refused.
- `run_command` runs without `sandbox-exec`, with the network; still the key-free environment; still an
  approval every time (the allowlist doesn't apply).
- New action `screenshot` (no args): after approval, a picture of the screen (`screencapture -x` on macOS;
  elsewhere the step says it isn't supported), given to the model like `view_file`.
- New action `open` (`target`: a path, an `https://` link, or an app name): after approval, opened with
  `open` (macOS), `xdg-open` (Linux) or `start` (Windows), never through a shell.
- The run shows a red **Computer access** badge, and its session records that it was on.

## Run options

`POST /api/agent/start` also takes:
- `computer` (bool, default false): Computer access for this run; `true` also needs `"confirmed": true`
  (the page sends it once you've confirmed the warning).
- `fast` (bool): at most 10 steps and the model's lowest effort Ixel knows for it.
- `effort` (one of Ixel's levels or ""): passed to the model on every call.
- `follows` (a run id): a follow-up. The prompt starts with that run's goal, its answer and its last few
  steps, so the conversation goes on. The page sends it for a message typed under a finished run.
- `uploads` (list of upload ids from `POST /api/agent/upload`).

`POST /api/agent/upload` (multipart, one file per request, field `file`) → `{"id", "name", "kind", "size"}`.
`GET /api/agent/run` adds `"computer": bool`, `"follows": id or null`, `"uploads": [{"id", "name", "kind"}]`,
and on a step whose action showed pictures, `"pictures": n`.

## The page (simple and clean, like a chat with an agent)

- **One column, a conversation.** Your message on the right; the agent's work under it as one quiet
  "Working…" line that expands into short rows (an icon and a verb: "Read greet.py", "Searched for 'TODO'",
  "Looked at photo.jpg", "Edited greet.py"), collapsed to "Worked for 40 s · 6 steps" when the run ends;
  then its answer as plain text (markdown). Approvals appear in the conversation as a card with the diff or
  the exact command and Approve / Deny (and Always allow for commands without Computer access).
- **The composer** at the bottom: a text box ("Ask the agent to do something…"), the attach button, and
  chips under it for the folder, the model, Fast, and Computer access (red when on). Enter sends,
  Shift+Enter is a new line. Typing under a finished run sends a follow-up.
- **The `/` menu**: typing `/` at the start opens a list you can filter and pick from with the arrows and
  Enter: `/new` (a new conversation), `/model NAME`, `/folder PATH`, `/fast` (on/off), `/effort LEVEL`,
  `/computer on|off`, `/attach`, `/memory`, `/stop`, `/help`.
- **History** behind a button at the top (a list of earlier conversations); **Memory and skills** behind
  another, as now. No side column.
- Light and dark, the app's variables, no new libraries, nothing kept in browser storage.

# Beta 3: open-source tools as add-on cards

The idea: rather than Ixel writing code for every outside tool (REA, Diagram Design…), the agent fills in a
short **add-on card** (`addon.toml`, the format in `docs/addon-cards.md` on the open-source add-ons branch) and
the person decides in Settings › Add-ons whether to turn it on. The agent never turns a card on.

## Adding a tool from a GitHub link

New actions (they work with or without Computer access):

- `fetch_repo` (`url`: `https://github.com/<owner>/<repo>`, optional `commit`): **after approval**, downloads
  `https://codeload.github.com/<owner>/<repo>/tar.gz/<commit or HEAD>` (no git, no shell) into the agent's
  folder under `scratch/<owner>-<repo>/`, extracting regular files only (refusing absolute paths, `..`, links
  and devices; at most 200 MB and 20,000 files). The result: the full commit sha (resolved with GitHub's API
  `GET /repos/<owner>/<repo>/commits/<ref>` when no commit was given), the top-level files, the README's first
  lines, and for a `subdir` the agent names later, the tree hash and file count per the card spec. The agent
  reaches the files as `scratch:<owner>-<repo>/PATH` in `list_dir`, `read_file`, `search` and `view_file`.
- `tree_hash` (`repo`: the scratch name, `subdir`): the card spec's `tree_sha256` and `files` for that
  folder, computed by Ixel (never by the model).
- `run_command` takes an optional `cwd`: `scratch:<owner>-<repo>` runs the approved command in that scratch
  copy (sandboxed on macOS; the network only with Computer access), to try the tool.
- `write_addon_card` (`id`, `card`: the TOML text): Ixel checks the card against every rule in the spec
  (unknown keys, ids, kinds, the commit and hash shapes, the repo URL, subdir, commands and placeholders)
  and that `[source]`'s `commit`, `tree_sha256` and `files` match what `fetch_repo`/`tree_hash` computed for
  that run. **After approval** (the card is shown in full) it's written to `~/.ixel/addons/<id>/addon.toml`.
  It refuses ids of built-in cards. Writing over an existing card is allowed only after approval and the
  approval card says the add-on must be approved again in Settings.
- The run's answer tells the person to open **Settings › Add-ons** to review and approve it.
- A starter skill `add-tool-from-github` walks the model through it: fetch, read the README and
  entry points, pick the kind (an `SKILL.md` folder is `skill`, an MCP server is `mcp`, else `cli`), pick
  the narrowest permissions, compute the hash, try it in scratch, write the card, finish.

## Using approved MCP add-ons

- Cards with `kind = "mcp"` that are switched on in Settings › Add-ons (asked through
  `ixel_mat.addons.enabled_cards("mcp")`; a missing module means none) are offered to the agent: the prompt
  lists each one's tools (name, description, input schema), found by starting the server once per run.
- New action `use_tool` (`addon`, `tool`, `arguments`): calls that tool over MCP stdio and returns its text
  (pictures it returns go to the model like `view_file`'s). The server is started from the card's one
  `commands` entry as an argv list, never a shell, `{addon}` filled with the card's installed files and
  `{output}` with the run's workspace; with the key-free environment; under `sandbox-exec` on macOS with the
  network allowed only when the card says `network = true`, and writes only in the workspace (`files =
  "output-folder"` or `"chosen-folder"`) or nowhere (`"none"`). The first call to each add-on in a run asks
  for approval (showing the add-on, the tool and its arguments); "Always allow" covers that add-on for the
  rest of the run. Servers are stopped when the run ends.
