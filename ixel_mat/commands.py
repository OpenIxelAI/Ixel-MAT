from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandDef:
    name: str
    description: str
    mode: str  # cli | mat | both
    usage: str
    aliases: tuple[str, ...] = ()
    group: str = ''  # the heading /help lists it under


ASK, LOOK_BACK, SESSION = 'Ask the panel', 'Look back', 'This session'

COMMANDS = [
    CommandDef('run', 'Launch the multi-agent terminal', 'cli', 'ixel', aliases=('',)),
    CommandDef('setup', 'Interactive setup — configure agents + API keys', 'cli', 'ixel setup', aliases=('configure',)),
    CommandDef('review', 'Ask the panel from the shell: answers, peer review, verdict (--diff reviews your changes)',
               'cli', 'ixel review "question"'),
    CommandDef('ask', 'One model answers on its own, no panel (--list shows who you can ask)', 'cli',
               'ixel ask --agent NAME "question"'),
    CommandDef('image', 'Make pictures with xAI (Grok) or OpenAI from a description, or from your files', 'cli',
               'ixel image "description" [-f FILE]', aliases=('images',)),
    CommandDef('gui', 'Open the panel in your browser (only this computer can reach it)', 'cli', 'ixel gui'),
    CommandDef('app', 'Open Ixel in a window of its own (it stops when you close the window)', 'cli', 'ixel app'),
    CommandDef('machines', 'Your SSH machines: list them, connect to one, or import ~/.ssh/config or Ixel Console',
               'cli', 'ixel machines [connect NAME | import ssh|console]', aliases=('machine',)),
    CommandDef('mcp', 'Run as a plugin for Claude Desktop, Claude Code, Codex… (`--setup` shows how)', 'cli', 'ixel mcp [--setup]'),
    CommandDef('saves', 'Usage saver scoreboard, and what reviews have cost', 'cli', 'ixel saves'),
    CommandDef('triage', 'Check the optional Triage helper: settings, key, and a test call', 'cli', 'ixel triage'),
    CommandDef('status', 'Single-screen health dashboard for providers, agents, and secrets', 'cli', 'ixel status'),
    CommandDef('model', "See each agent's model, or change one (latest = always the newest)", 'cli',
               'ixel model [agent] [model]', aliases=('models',)),
    CommandDef('config', 'Show resolved config, tokens, validation status', 'cli', 'ixel config', aliases=('cfg',)),
    CommandDef('agents', 'List agents + test connectivity', 'cli', 'ixel agents'),
    CommandDef('doctor', 'Check each part of Ixel; --check tests your models too', 'cli', 'ixel doctor [--check] [--json]'),
    CommandDef('update', 'Get the latest Ixel (`--check` only says whether one is out)', 'cli', 'ixel update [--check]',
               aliases=('upgrade',)),
    CommandDef('docs', "Open Ixel's docs in your browser (ixelai.com/docs)", 'cli', 'ixel docs [--no-browser]'),
    CommandDef('version', 'Show version', 'cli', 'ixel version', aliases=('v',)),
    CommandDef('help', 'Show this help', 'cli', 'ixel help', aliases=('h',)),
    CommandDef('full', 'Every agent answers, side by side (no review)', 'mat', '/compare <prompt>',
               aliases=('compare',), group=ASK),
    CommandDef('review', 'Models answer, review each other anonymously, then agree on a verdict (--diff: your changes)',
               'mat', '/review [--quick|--deep] <question>', group=ASK),
    CommandDef('saver', 'Save usage: cheaper models draft and check, your big model only verifies', 'mat', '/saver <question>', group=ASK),
    CommandDef('auto', 'Let Triage pick quick, review or deep for this question ([triage] in your config)', 'mat',
               '/auto <question>', group=ASK),
    CommandDef('saves', 'Usage saver scoreboard, and what reviews have cost', 'mat', '/saves', group=LOOK_BACK),
    CommandDef('answers', 'Show the full answers from the last /review', 'mat', '/answers', group=LOOK_BACK),
    CommandDef('new', "Start a fresh topic: the next question won't see the earlier ones", 'mat', '/new', group=LOOK_BACK),
    CommandDef('config', 'Show resolved config, tokens, validation status', 'mat', '/config', aliases=('cfg',), group=SESSION),
    CommandDef('agents', 'List agents + test connectivity', 'mat', '/agents', group=SESSION),
    CommandDef('docs', "Open Ixel's docs in your browser (ixelai.com/docs)", 'mat', '/docs', group=SESSION),
    CommandDef('help', 'Show this help', 'mat', '/help', aliases=('h',), group=SESSION),
    CommandDef('quit', 'Exit', 'mat', '/quit', aliases=('exit', 'q'), group=SESSION),
]


def _mode_matches(cmd: CommandDef, mode: str) -> bool:
    return cmd.mode == mode or cmd.mode == 'both'


def build_help_rows(mode: str) -> list[tuple[str, str]]:
    rows = []
    for cmd in COMMANDS:
        if _mode_matches(cmd, mode):
            rows.append((cmd.usage, cmd.description))
    return rows


def build_help_groups(mode: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """build_help_rows, under the headings the commands belong to (commands without one come last)."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for cmd in COMMANDS:
        if _mode_matches(cmd, mode):
            groups.setdefault(cmd.group, []).append((cmd.usage, cmd.description))
    return sorted(groups.items(), key=lambda item: item[0] == '')


def resolve_command_name(name: str, mode: str):
    raw = name.strip().lstrip('/')
    if not raw and mode == 'cli':
        return 'run'

    exact = []
    candidates = []
    for cmd in COMMANDS:
        if not _mode_matches(cmd, mode):
            continue
        names = (cmd.name, *cmd.aliases)
        if raw in names:
            exact.append(cmd.name)
        if any(n.startswith(raw) for n in names if n):
            candidates.append(cmd.name)

    if exact:
        return exact[0]
    uniq = sorted(set(candidates))
    if len(uniq) == 1:
        return uniq[0]
    if len(uniq) > 1:
        return ('ambiguous', uniq)
    return None
