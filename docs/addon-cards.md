# Add-on cards

Ixel adds outside tools as **switches on one Add-ons page**, not as tabs. Each add-on is described by
one small file, its card (`addon.toml`). Ixel reads the card, shows the switch, asks you to approve
what the add-on may do, installs exactly the pinned version, and puts it where it's used (Ask, the
Board or the agent). The agent can write cards for new tools; it can never turn one on.

## Where cards live

| Who wrote it | Where | Can be turned on |
|---|---|---|
| Ships with Ixel | `ixel_mat/addons/<id>.toml` | After the approval dialog |
| Written by the agent (or by hand) | `~/.ixel/addons/<id>/addon.toml` | After the approval dialog, which also says "written by the agent, not reviewed" |

An add-on's files are installed next to its card: `~/.ixel/addons/<id>/files/`. Turning it off removes
`files/` (and an agent-written card stays, switched off, until removed).

## The card

```toml
id = "diagram-design"            # a-z, 0-9 and "-", 2-40 characters; unique
name = "Diagram Design"
summary = "Diagrams as one HTML file, in your brand."
by = "Cathryn Lavery"
license = "MIT"                  # SPDX id, shown on the switch
homepage = "https://github.com/cathrynlavery/diagram-design"
kind = "skill"                   # "skill" | "mcp" | "cli"

[source]
repo = "https://github.com/cathrynlavery/diagram-design"   # https://github.com/<owner>/<repo> only
commit = "7a2e22170ee838e859d8142ba825d83f8b6375e6"        # full 40-character sha
subdir = "skills/diagram-design"  # what gets installed; "" for the whole repo
tree_sha256 = "e4adecc695f6367ce202995133e6e1fe5d18b6e4e142a06bd30926d72bfbb4a1"
files = 279

[permissions]
files = "output-folder"          # "none" | "output-folder" | "chosen-folder"
network = false                  # may it use the internet when it runs
commands = [                     # exact programs Ixel may run for it; [] for most skills
  ["python3", "{addon}/scripts/self_check.py", "{output}"],
]

[use]
where = "ask"                    # "ask" | "board" | "agent"
trigger = "/diagram"             # optional; a word starting with "/"
output = "html"                  # "text" | "html" | "file" | "image"
```

### What each kind means
- **skill**: instructions and templates (an Agent Skill folder with a `SKILL.md`). Ixel or the agent
  reads them; nothing runs except the `commands` the card lists.
- **mcp**: a tool server. When on, Ixel can hand it to the agent and Board workers that accept tools.
  Its `commands` entry is the one way to start it.
- **cli**: a command-line program Ixel runs with the card's `commands`, filling in the inputs.

### Rules the loader enforces
- Unknown keys anywhere are refused, with the key named, so a typo never silently loosens anything.
- `id`, `kind`, `files`, `where` and `output` must be one of the values above.
- `commit` is 40 hex characters; `tree_sha256` is 64; `files` is a positive integer.
- `repo` is `https://github.com/<owner>/<repo>`; `homepage` is an https URL.
- `subdir` is relative, with no `..`, no leading `/`, no backslashes.
- Each command is a list of strings, run as an argv list, never through a shell, so `;`, `|`, `$()` and
  the like are plain characters. Placeholders allowed: `{addon}` (the installed files), `{input}`,
  `{output}`. Any other `{...}` is refused. The first item must be a bare program name or start with
  `{addon}/`.
- `network = true`, `files = "chosen-folder"` or any command makes the approval dialog say so in
  plain words before anything is installed.

### Installing
Download `https://codeload.github.com/<owner>/<repo>/tar.gz/<commit>`, extract only `subdir` into a
temporary folder (refusing absolute paths, `..`, links and devices), then check the tree hash: for each
regular file, take its path relative to `subdir` with `/` separators and the sha256 hex of its bytes;
sort by path; join as `"{path}\0{sha}\n"`; sha256 the UTF-8 result. It must equal `tree_sha256` and the
file count must equal `files`. Only then move it into `~/.ixel/addons/<id>/files/`. Anything else is
refused and nothing is left behind.

## Adding a tool from a GitHub link (the agent)
The agent reads the repository, decides the kind, writes a card in this format to
`~/.ixel/addons/<id>/addon.toml` (with the commit it read and the tree hash it computed), tries it in a
scratch folder, and tells the person to open Settings › Add-ons to review and approve it. It never
turns a card on, never edits a built-in card, and never widens permissions on a card the person
already approved; changed permissions need a fresh approval.

## State and approval

Ixel keeps the on/off and approval state in `~/.ixel/addons/state.json`, one entry per card id:

```json
{"diagram-design": {"on": true, "approved_card_sha256": "<sha256 of the card file's bytes>",
                    "approved_tree_sha256": "<the card's tree_sha256 when approved>",
                    "approved_at": "2026-10-12T03:40:00Z"}}
```

Only the Settings approval route writes it. A card counts as **enabled** only when it is switched on,
its current file's sha256 equals `approved_card_sha256`, its `tree_sha256` equals
`approved_tree_sha256`, and its files are installed. Any edit to a card file, even a single byte, means
it needs approving again: it shows as "changed since you approved it" and stays off until approved.
The installed files' tree hash is checked when installing and when the switch is turned on.

## Python API (for the agent and the rest of the app)

`ixel_mat.addons`:
- `validate_card_text(text: str) -> Card`: parse and check a card; `ValueError` with the loader's own
  message on any problem.
- `all_cards() -> list[Card]`: every card, built-in and agent-written, with its state.
- `enabled_cards(kind: str | None = None) -> list[Card]`: only enabled cards (rules above), optionally
  of one kind.
- `write_agent_card(text: str) -> Card`: validate, refuse a built-in id, and write
  `~/.ixel/addons/<id>/addon.toml`. This is the agent's only way to add or change a card; it never
  writes `state.json`. Overwriting a card changes its sha256, so it needs approving again.
- `fill_command(card: Card, argv: list[str], *, input: str | None = None, output: str | None = None) -> list[str]`:
  fill `{addon}`, `{input}` and `{output}`. Refuses a placeholder the call didn't supply.

`Card` fields: `id`, `name`, `summary`, `by`, `license`, `homepage`, `kind`, `builtin` (bool),
`card_path` (the toml), `dir` (`~/.ixel/addons/<id>/`, for built-in cards too), `files_dir`
(`dir / "files"`), `source` (`repo`, `commit`, `subdir`, `tree_sha256`, `files`), `permissions`
(`files`, `network`, `commands`: a list of argv lists with placeholders unfilled), `use` (`where`,
`trigger`, `output`), and state: `on`, `approved`, `changed_since_approval`, `installed`, `enabled`.
