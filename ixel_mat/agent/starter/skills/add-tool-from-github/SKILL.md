---
name: add-tool-from-github
description: Use it when the user gives a GitHub link to a tool they want in Ixel.
---

1. Fetch the repository using fetch_repo with the provided GitHub link.
2. Read the README and entry points (scratch:NAME paths) from the repository.
3. Decide the kind: "skill" if there's a SKILL.md, "mcp" if it's an MCP server, otherwise "cli".
4. Choose the narrowest permissions: files (none or output-folder), network only if internet is needed, and only required commands.
5. Use tree_hash on the folder to install.
6. Try the tool using run_command in scratch:NAME. If it needs a build or the network, say so in the answer; don't keep retrying.
7. Write the card with write_addon_card, using the commit and tree hash from steps 1 and 5. Use exactly this shape, every key shown and no others (`terms` is the only optional one):

```toml
id = "example-tool"              # a-z, 0-9 and "-", 2-40 characters
name = "Example Tool"
summary = "One plain sentence: what it does for the user."
by = "The author or organisation"
license = "MIT"                  # the SPDX id from the repo's license
homepage = "https://github.com/owner/repo"
kind = "mcp"                     # "skill" | "mcp" | "cli"
terms = "Optional: a short note on licence or terms limits."

[source]
repo = "https://github.com/owner/repo"
commit = "<the full 40-character commit fetch_repo gave>"
subdir = ""                      # the folder to install, "" for the whole repo
tree_sha256 = "<from tree_hash>"
files = 123                      # from tree_hash

[permissions]
files = "output-folder"          # "none" | "output-folder" | "chosen-folder"
network = false                  # true only if it must use the internet
commands = [["node", "{addon}/scripts/start.mjs", "--mcp"]]   # argv lists; only {addon}, {input}, {output}

[use]
where = "agent"                  # "ask" | "board" | "agent"
output = "text"                  # "text" | "html" | "file" | "image"
```
8. Finish by telling the user to open Settings > Add-ons to review and approve it, and what it still needs (a build step, the network).

This skill never turns a card on. Only the user does, in Settings > Add-ons.
