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

## Usage

Each model call is recorded the way `ixel ask` records one (`call_usage`), and the run shows the total.
