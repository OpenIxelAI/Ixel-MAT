"""
Machines: your servers over SSH, and the OpenClaw or Hermes agent on each (what Ixel Console did).

- store.py: the machines you saved (~/.config/ixel-mat/machines.json), and importing them from
  ~/.ssh/config or from Ixel Console.
- ssh.py: the ssh lines, and each server's host key, pinned in Ixel's own file and enforced by ssh.
- terminal.py: Connect opens a terminal window running ssh.
- runs.py: one command, typed by you, run on several machines at once.

It runs only what the person typed or picked: no model ever runs anything here. It uses Ixel's
settings folder and a few process helpers, nothing from the panel, so it can be split out later.
"""
