# Roadmap

Last updated: 2026-10-04

## Shipped in 0.3

- **The review engine:** anonymous peer review, concessions, agreement, a moderated verdict; `quick`,
  `review` and `deep` modes.
- **Usage saver:** cheaper models draft and review, a big model verifies, sends wrong drafts back to be
  fixed (or fixes them itself), and is skipped when every draft was rated correct. Per-agent and
  per-call thinking levels.
- **Saves counter:** saves, streaks, a per-model leaderboard, and achievements in the terminal, the
  browser app and `ixel saves`.
- **Three front ends:** terminal (`/review`, `/saver`, `ixel review`), browser app (`ixel gui`), and a plugin
  for Claude Desktop, Claude Code, Codex and Cursor (`ixel mcp`).
- **Subscriptions, not just API keys:** answer-only presets for Claude Code, Codex, Gemini CLI, GitHub
  Copilot and OpenCode, attacked by a test you can run (on macOS or Linux) against each new release.
- **Local models:** Ollama and LM Studio detected by the setup wizard, with no key needed.
- **Everywhere:** installers for macOS, Linux and Windows; Python 3.10–3.14. The Linux installer has been
  tried on Ubuntu, Debian, Fedora and Arch.
- **A security pass** over the whole codebase. See [SECURITY.md](SECURITY.md).

## Shipped since 0.3

- **Streaming verdicts:** the verdict shows as it's written (Claude Code and API-key moderators).
- **Follow-ups:** the panel sees your last three exchanges.
- **No waiting on the slowest model:** with three or more models, a slow one gets as long again as the
  others took, then the review goes on without it.
- **Private installs:** `pipx` / `uv` from git, kept current by `ixel update`.
- **A reworked browser app.**
- **Triage (optional):** auto mode picks quick, review or deep per question, and can skip checking a
  question doesn't need, using TypeSafe or one of your own models.
- **Defined review grades and a confidence check:** the moderator's confidence is checked against the
  reviews, and the saves counter counts big-model checks.
- **Code-aware review:** `--diff`, `--staged`, `--base REF` and `--file` (and a Code box in the browser, and
  `code` in the plugin). Read-only, fenced, with secrets refused and hidden characters shown.
- **Cost tracking:** every review says what it used and cost (API keys priced, subscriptions and local
  models counted), and `ixel saves` totals it by month next to what saver mode saved.
- **CLI agents on Windows:** npm-installed CLIs start without going through `cmd.exe`, programs are found
  on PATH only, and long prompts go on stdin.
- **Ixel as an app:** `ixel app`, and **Ixel** in the Start Menu, Applications or the app menu. On Windows
  it's an Edge or Chrome app window (nothing new to install, so Smart App Control has nothing to block), on
  a Mac a native Ixel.app built at install, on Linux a GTK window. It stops when you close it.
- **A better terminal:** completion, history, multi-line input and folded pastes at the prompt; Ctrl+C cancels a
  running review; a welcome card with the logo; a live view that scrolls finished rounds into your history.
- **The one Ixel app:** Ask, Board, Machines, Health and Settings in one window, down a rail on the left.
  - **Ask:** the panel, with pictures, video and sound in a question (pictures go only to models that can
    see them; sound is written out into the question).
  - **Board:** Handoff's task board for a project, with its pull requests listed above the columns, to
    review or fix one through Handoff.
  - **Machines:** your SSH servers and the agents on them, replacing Ixel Console.
  - **Health:** whether each part of Ixel is ready, with the line that fixes it (`ixel doctor` in the
    terminal).
  - **Settings:** the panel, modes, Saver, Triage, pictures, sound and keys.
- **Pictures:** `ixel image` makes them with xAI or OpenAI, and `ixel ask` gives a question to one model.

## Next

### More ways in
- **Cursor and Cline CLIs:** add presets once a live test proves each can be locked to answer-only.
  (Cline auto-approves every tool by default.)
- **Out of usage in a review:** `ixel ask --agent a,b,c` moves to the next model you named when one is out
  of usage; a review could do the same for a panel member, and Handoff for a task's worker.
- **Packaging:** `pipx install ixel-mat` / `uv tool install ixel-mat` from PyPI, a Homebrew formula, and
  signed release builds, so installing doesn't need a git checkout. (Installs from git already work.)

### Better reviews
- **Review from a git hook or CI:** a `pre-push` hook and an exit code for "the panel found a problem",
  building on `ixel review --diff --json`.
- **Prices for other providers:** a maintained table for OpenAI, Google and xAI models, not just Claude's.
- **Smarter saver routing:** learn which drafters get accepted for which kinds of questions and ask them
  first.

### Agents that act (carefully)
Letting the panel edit files or run commands is the most requested feature, and the riskiest. The plan:
- The panel proposes changes as a patch. **You** approve each one, and Ixel applies it. No model gets a
  shell.
- When a command has to run, it runs in a real sandbox (container or OS sandbox) with no network and no
  access to your keys, and you approve the exact command first.
- The same attack tests as the CLI presets, written first.

### Handoff (separate project)
[Handoff](https://github.com/OpenIxelAI/Handoff-by-IxelAI) is its own project now: a shared task board
your agents read and write through MCP ("Codex, take the tests; Claude, review them"), with Ixel's models
answering and reviewing. Its board is in the Ixel app, and `/handoff` in Ask splits one request across
your agents. It's kept separate so Ixel stays a small, local, answer-only tool.

## Won't do

- **A network-reachable server.** Ixel stays on your machine; that's most of why it's safe to run.
- **Reading project config from the current folder.** Agents can name commands to run.
