---
name: add-tool-from-github
description: Use it when the user gives a GitHub link to a tool they want in Ixel.
---

1. Fetch the repository using fetch_repo with the provided GitHub link.
2. Read the README and entry points (scratch:NAME paths) from the repository.
3. Decide the kind: "skill" if there's a SKILL.md, "mcp" if it's an MCP server, otherwise "cli".
4. Choose the narrowest permissions: files (none or output-folder), network only if internet is needed, and only required commands.
5. Use tree_hash on the folder to install.
6. Try the tool using run_command in scratch:NAME.
7. Write the card with write_addon_card, using the commit and tree hash from steps 1 and 5.
8. Finish by telling the user to open Settings > Add-ons to review and approve it.

This skill never turns a card on. Only the user does, in Settings > Add-ons.
