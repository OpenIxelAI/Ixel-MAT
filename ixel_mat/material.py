"""
Code for the panel to review: a git diff, files you name, or text you paste. Read-only.

Ixel reads it once, before the panel starts: it runs nothing in your project and changes
nothing, and the models get no file or shell access. The code goes into every round's prompt
fenced with the run's random marker, like everything else the user or a model wrote, so a
comment in it saying "ignore your instructions" is just more code to review.

It's kept apart from the question: the question is what's saved for follow-ups and what Triage
sees, and the code shouldn't travel there.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ixel_mat.agents.launch import NO_WINDOW_FLAGS, find_on_path

# Every model reads it in every round, so this is per call, and a review makes many calls
MAX_MATERIAL_CHARS = 60_000
MAX_FILES = 20

DEFAULT_CODE_QUESTION = ("Review this change. Find bugs, security problems and anything that would break, "
                         "and say what should change before it's pushed. If it looks right, say so.")
DEFAULT_FILES_QUESTION = ("Review this code. Find bugs, security problems and anything that would break, "
                          "and say what should change. If it looks right, say so.")

# Generated files that make a diff huge and aren't worth a model's reading
LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "poetry.lock",
             "uv.lock", "Pipfile.lock", "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum")
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's id for "no files": the base of a first commit


class MaterialError(ValueError):
    pass


@dataclass
class Material:
    title: str                   # what it is, as the models see it
    text: str
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # for the user: what was left out, and why

    @property
    def chars(self) -> int:
        return len(self.text)

    def summary(self) -> str:
        files = f"{len(self.files)} file{'s' if len(self.files) != 1 else ''}, " if self.files else ""
        return f"{files}{self.chars:,} characters"

    def to_dict(self) -> dict:
        return {"title": self.title, "files": list(self.files), "chars": self.chars}


# ── Checks ────────────────────────────────────────────────────────────────────

# Characters that make code read differently from what it does ("Trojan Source"), or that
# can't be seen at all: bidi controls, zero-width and other invisible characters (Hangul fillers,
# which can name a JavaScript variable nobody sees, variation selectors, which can carry hidden
# bytes, and line separators, which JavaScript breaks lines at), Unicode tag characters (which can
# spell out hidden text), and control codes, which a terminal-safe cleanup would otherwise delete
# along with the text after them. Shown to the models as [U+202E] rather than removed, so they can
# point them out. A lone carriage return is a line break to Python and JavaScript, so it's shown
# and kept as one.
_HIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5"
                     "\u180b-\u180f\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u206f\u2800\u3164"
                     "\ufe00-\ufe0f\ufeff\uffa0\ufff9-\ufffb\U0001d173-\U0001d17a\U000e0000-\U000e007f"
                     "\U000e0100-\U000e01ef]")


def mark_hidden_characters(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "[U+000D]\n")
    return _HIDDEN.sub(lambda m: f"[U+{ord(m.group()):04X}]", text)


_SECRETS = [
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|PuTTY-User-Key-File-\d+:")),
    # After any separator, a key's own underscore included ("OPENAI_KEY_sk-…")
    ("an API key", re.compile(r"(?<![A-Za-z0-9])sk-(?:ant-|proj-|or-)?[A-Za-z0-9_-]{24,}")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # The secret half has no prefix of its own: caught next to its usual name
    ("an AWS secret key", re.compile(r"(?i)aws_?secret_?access_?key[\"']?\s{0,3}[:=]\s{0,3}[\"']?"
                                     r"[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}|\bxapp-\d-[A-Za-z0-9-]{20,}")),
    ("a Slack webhook", re.compile(r"hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]{20,}")),
    ("a Groq API key", re.compile(r"\bgsk_[A-Za-z0-9]{40,}")),
    ("a Google OAuth client secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}")),
    ("a login token (JWT)", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")),
    ("a Stripe secret key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{20,}")),
    ("an xAI API key", re.compile(r"\bxai-[A-Za-z0-9]{32,}")),
    ("an npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("a GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("a Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
]
_SECRET_FILE = re.compile(r"(?:^|/)(?:\.env(?:\.[^/]*)?|id_(?:rsa|dsa|ecdsa|ed25519)|[^/]*\.(?:pem|key|p12|pfx|ppk))$",
                          re.IGNORECASE)
_EXAMPLE_FILE = re.compile(r"\.(?:example|sample|template|dist)$", re.IGNORECASE)


def secret_file(name: str) -> bool:
    name = name.replace("\\", "/")
    return bool(_SECRET_FILE.search(name)) and not _EXAMPLE_FILE.search(name)


# Files whose name says they hold logins or tokens (package registries, git hosts, cloud tools): a new
# file git doesn't track yet with one of these names is never sent, whatever's in it
_CREDENTIAL_NAMES = frozenset({
    "credentials", "credentials.toml", "credentials.json", ".credentials.json", ".netrc", "_netrc",
    ".git-credentials", ".npmrc", ".pypirc", ".yarnrc.yml", ".envrc", ".pgpass", ".htpasswd", "auth.json",
    "hosts.yml", "settings.xml", "gradle.properties", "secrets.json", "secrets.yml", "secrets.yaml",
    "secrets.toml"})
_CREDENTIAL_SUFFIXES = (".keystore", ".jks", ".kdbx", ".gpg")


def credential_file(name: str) -> bool:
    base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return secret_file(name) or base in _CREDENTIAL_NAMES or base.endswith(_CREDENTIAL_SUFFIXES)


# Where a diff names a file: the new name, and the old one of a rename, copy or deletion
_DIFF_NAME = re.compile(r"(?:=== (.+) ===|\+\+\+ b/(.+)|--- a/(.+)|rename from (.+)|copy from (.+))$")


def mask_secrets(text: str, also: Iterable[str] = ()) -> str:
    """Text with anything that looks like a key blanked out: for errors a server sent back, which can echo one.
    also: keys known to be in play (the agent's own), blanked out whatever they look like."""
    for key in also:
        if key and len(key) >= 8:
            text = text.replace(key, "[key hidden]")
    for kind, pattern in _SECRETS:
        text = pattern.sub(f"[{kind.split(' ', 1)[1]} hidden]", text)
    return text


def find_secret(material: Material) -> str | None:
    """Where the material looks like it holds a secret (a key file, or a key in the text), if anywhere."""
    for name in material.files:
        if secret_file(name):
            return f"{name} looks like a secrets file"
    current = ""
    for number, line in enumerate(material.text.splitlines(), 1):
        header = _DIFF_NAME.match(line)
        if header:
            name = next(g for g in header.groups() if g)
            if secret_file(name):
                return f"{name} looks like a secrets file"
            if not line.startswith(("rename from", "copy from")):
                current = name
            continue
        for kind, pattern in _SECRETS:
            if pattern.search(line):
                return f"{current or 'the text'} has what looks like {kind}" + ("" if current else f" (line {number})")
    return None


def check(material: Material, allow_secrets: bool = False) -> Material:
    """Refuse material that's empty, too big for the panel, or holds what looks like a secret."""
    if not material.text.strip():
        raise MaterialError("There's no code to review.")
    # Measured as the models will see it (hidden characters shown as [U+…]), so nothing that
    # passes is cut off later
    size = len(mark_hidden_characters(material.text))
    if size > MAX_MATERIAL_CHARS:
        raise MaterialError(
            f"That's {size:,} characters of code, and the panel takes up to {MAX_MATERIAL_CHARS:,}: "
            "every model reads it in every round. Narrow it down, for example to the files that matter.")
    if not allow_secrets:
        found = find_secret(material)
        if found:
            raise MaterialError(
                f"Not sent: {found}. The code goes to every model on the panel, so take the secret out first "
                "(or leave that file out).")
    return material


# ── Where it comes from ───────────────────────────────────────────────────────

def pasted(text: str, title: str = "code or text the user attached") -> Material:
    return Material(title=title, text=text.replace("\r\n", "\n"))  # a lone \r is kept, and shown


# What a repository's own settings could otherwise make git run while it reads a diff: an
# fsmonitor, a pager, and (with no network) a fetch of missing objects in a partial clone.
# External diffs and textconv filters are switched off on the diff itself, and the
# repository's clean/smudge filters by name (_repo_filters, git_diff).
_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.quotepath=off", "-c", "protocol.allow=never", "--no-pager"]
# (No replace refs: a commit named by its id, as a sealed review names a pull request's, is that commit)
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "GIT_NO_LAZY_FETCH": "1",
        "GCM_INTERACTIVE": "never", "GIT_NO_REPLACE_OBJECTS": "1"}


def _run_git(cwd: str | os.PathLike | None, args: list[str], timeout: float) -> subprocess.CompletedProcess:
    from ixel_mat.config.secrets import child_env  # (without the keys Ixel holds: git and its filters don't need them)
    git = find_on_path("git")
    if not git:
        raise MaterialError("git isn't installed (or isn't on PATH), so there's no diff to read.")
    try:
        return subprocess.run([git, *_SAFE, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env={**child_env(nested=False), **_ENV},
                              stdin=subprocess.DEVNULL, creationflags=NO_WINDOW_FLAGS)
    except subprocess.TimeoutExpired:
        raise MaterialError(f"git {args[0]} took more than {timeout:g}s") from None
    except OSError as exc:
        raise MaterialError(f"Couldn't run git: {exc}") from None


def _git(cwd: str | os.PathLike | None, *args: str, timeout: float = 60) -> str:
    done = _run_git(cwd, list(args), timeout)
    if done.returncode != 0:
        detail = " ".join(line.strip() for line in done.stderr.strip().splitlines()[:4]) or "no details"
        raise MaterialError(f"git {args[0]} failed: {detail}")
    return done.stdout


def _repo_filters(top: str) -> list[str]:
    """The clean/smudge/process filters this repository defines for itself, by name.
    (Yours, from your own git config, such as Git LFS's, keep working.)"""
    done = _run_git(top, ["config", "--show-scope", "--name-only", "--get-regexp", r"^filter\."], 30)
    lines = done.stdout.splitlines()
    if done.returncode not in (0, 1):  # a git older than 2.26: no --show-scope
        done = _run_git(top, ["config", "--local", "--name-only", "--get-regexp", r"^filter\."], 30)
        lines = [f"local\t{line}" for line in done.stdout.splitlines()]
    names = set()
    for line in lines:
        scope, _, key = line.partition("\t")
        if scope in ("local", "worktree") and key.count(".") >= 2:
            names.add(key[len("filter."):key.rindex(".")])
    return sorted(names)


# What an attr pathspec can match a filter's name with (git refuses anything else there)
_FILTER_NAME = re.compile(r"[A-Za-z0-9_-]+")
_DIFF_FILE = re.compile(r"^diff --git a/.+ b/(.+)$", re.MULTILINE)


def _some(names: list[str], shown: int = 5) -> str:
    return ", ".join(names[:shown]) + (f" and {len(names) - shown} more" if len(names) > shown else "")


def _check_ref(top: str, ref: str | None) -> str:
    if not ref or ref.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in ref):
        raise MaterialError(f"{ref!r} isn't a branch or commit name.")
    try:
        _git(top, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except MaterialError:
        raise MaterialError(f"There's no branch or commit named {ref!r} here.") from None
    return ref


def git_diff(what: str = "uncommitted", base: str | None = None, cwd: str | os.PathLike | None = None,
             new_files: bool = False, head_ref: str | None = None) -> Material:
    """
    what = "uncommitted": every change since the last commit, staged or not (git diff HEAD)
           "staged":      only what's staged (git diff --cached)
           "base":        everything since this branch left `base`, uncommitted changes included; with
                          `head_ref`, what's committed on it since it left `base` (a pull request, say,
                          fetched but not checked out: your own files and changes aren't read)
    New files git doesn't track yet aren't in a diff; notes says which. With new_files (not for
    "staged", nor with `head_ref`), the ones that are safe to send are added as new files, while there's
    room (_new_files).
    """
    if head_ref is not None and (what != "base" or new_files):
        raise ValueError("head_ref goes with what='base', and without new_files")
    try:
        top = _git(cwd, "rev-parse", "--show-toplevel").strip()
    except MaterialError as exc:
        if "not a git repository" not in str(exc).lower():
            raise  # git is missing, or refuses this folder (and says why)
        raise MaterialError("This isn't inside a git repository, so there's no diff. "
                            "Name the files to review instead.") from None
    try:
        _git(top, "rev-parse", "--verify", "--quiet", "HEAD")
        head = "HEAD"
    except MaterialError:
        head = _EMPTY_TREE  # no commits yet: everything added is new
    if what == "staged":
        spec, title = ["--cached"], "the staged changes (git diff --cached)"
    elif what == "base" and head_ref is not None:
        _check_ref(top, base)
        _check_ref(top, head_ref)
        try:
            merge_base = _git(top, "merge-base", base, head_ref).strip()
        except MaterialError:
            raise MaterialError(f"{head_ref} and {base} have no history in common, so there's no diff between "
                                "them.") from None
        spec = [merge_base, head_ref]
        title = f"the changes on {head_ref} since it left {base} (git diff {base}...{head_ref})"
    elif what == "base":
        _check_ref(top, base)
        merge_base = _git(top, "merge-base", base, head).strip() if head == "HEAD" else _EMPTY_TREE
        spec = [merge_base]
        title = (f"everything on this branch since it left {base}, changes not yet committed included "
                 f"(git diff {base}...)")
    elif what == "uncommitted":
        spec, title = [head], "the changes not yet committed (git diff HEAD)"
    else:
        raise ValueError(f"unknown diff {what!r}")
    # The repository's own filters are emptied, so they run nothing, and not required, or git stops at
    # the first file they filter (git-crypt, `git lfs install --local`). Without its filter git reads such
    # a file as it is on disk, which for git-crypt is the secret in plain text, so those files are left out.
    filters = _repo_filters(top)
    odd = [name for name in filters if not _FILTER_NAME.fullmatch(name)]
    if odd:
        raise MaterialError(f"This repository's git config has a filter named {odd[0]!r}, and git can't leave "
                            "the files it stores out of a diff, so there's no diff to read. "
                            "Name the files to review instead.")
    off = [arg for name in filters for setting in ("clean=", "smudge=", "process=", "required=false")
           for arg in ("-c", f"filter.{name}.{setting}")]
    excludes = ([f":(exclude,glob)**/{name}" for name in LOCKFILES]
                + [f":(exclude,attr:filter={name})" for name in filters])
    # Fixed a/ and b/ prefixes (whatever diff.noprefix says), so every file name is found; submodules
    # as commit ids only, so git doesn't go into them
    text = _git(top, *off, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M",
                "--src-prefix=a/", "--dst-prefix=b/", "--submodule=short", "--ignore-submodules=dirty",
                *spec, "--", ".", *excludes)
    notes, filtered = [], []
    if filters:
        filtered = [f for f in _git(top, *off, "diff", "--name-only", "-z", "--no-renames", *spec, "--",
                                    *[f":(attr:filter={name})" for name in filters]).split("\0") if f]
        if filtered:
            notes.append("Not included, because a filter in this repository's git config (such as git-crypt's "
                         f"or Git LFS's) stores them, and Ixel doesn't run it: {_some(filtered)}.")
    if what != "staged" and head_ref is None:  # (with head_ref, your own files aren't what's reviewed)
        untracked = [f for f in _git(top, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if f]
        if untracked and new_files:
            added, left_out = _new_files(top, untracked, MAX_MATERIAL_CHARS - len(mark_hidden_characters(text)))
            if added:
                text = (text if not text or text.endswith("\n") else text + "\n") + added
                title = title.replace(" (git diff", ", new files included (git diff", 1)
            if left_out:
                notes.append(f"New files left out: {_some(left_out, 20)}.")  # each with why, up to 20
        elif untracked:
            notes.append(f"Not included, because git doesn't track them yet: {_some(untracked)}. "
                         "`git add` them (or name them as files) to include them.")
    if not text.strip():
        raise MaterialError(" ".join([f"There are no changes to review in {title}.", *notes]))
    files = _DIFF_FILE.findall(text)
    if set(files) & set(filtered):  # a git that didn't leave them out after all
        raise MaterialError("git didn't leave out the files this repository's own filters store (an old git?), "
                            "so the diff isn't sent. Update git, or name the files to review instead.")
    return Material(title=title, text=text, files=files, notes=notes)


# A review of new files reads at most this many of them; a folder of generated files isn't worth the wait
MAX_NEW_FILES = 200


def _new_file_diff(name: str, text: str) -> str:
    """A new file, written the way git diff shows one."""
    head = f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n+++ b/{name}\n"
    if not text:
        return head
    lines = text.split("\n")
    complete = lines[-1] == ""
    if complete:
        lines.pop()
    body = "".join(f"+{line}\n" for line in lines)
    return head + f"@@ -0,0 +1,{len(lines)} @@\n" + body + ("" if complete else "\\ No newline at end of file\n")


def _new_files(top: str, names: list[str], room: int) -> tuple[str, list[str]]:
    """
    New files git doesn't track yet, as a diff that adds them, while they fit in `room` characters (as
    the models see them). Never sent: a link, anything but a plain text file, a file whose name says it
    holds keys or logins, a lockfile, one this repository's .gitattributes gives a filter (git-crypt's,
    Git LFS's), and one with what looks like a key in it. Returns the diff, and what was left out and why.
    """
    looked, rest = names[:MAX_NEW_FILES], names[MAX_NEW_FILES:]
    filtered: set[str] = set()
    for start in range(0, len(looked), 50):  # a short command line, on Windows too
        chunk = looked[start:start + 50]
        out = _run_git(top, ["check-attr", "-z", "filter", "--", *chunk], 30)
        if out.returncode != 0:
            filtered.update(chunk)  # can't tell, so none of them go
            continue
        fields = out.stdout.split("\0")
        for i in range(0, len(fields) - 2, 3):
            if fields[i + 2] not in ("unspecified", "unset"):
                filtered.add(fields[i])
    root = Path(top).resolve()
    parts: list[str] = []
    left_out: list[str] = []
    for name in looked:
        path = Path(top, name)
        why = None
        if credential_file(name):
            why = "its name says it holds keys or logins"
        elif name.replace("\\", "/").rsplit("/", 1)[-1] in LOCKFILES:
            why = "a lockfile"  # left out of diffs too: long, generated, and rarely worth a review
        elif name in filtered:
            why = "the repository's .gitattributes gives it a filter"
        else:
            try:
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode):
                    why = "it's a link"
                elif not stat.S_ISREG(info.st_mode):
                    why = "it isn't a file"
                elif info.st_size > room * 4:
                    why = "too big"
                else:
                    path.resolve().relative_to(root)
                    data = path.read_bytes()
            except (OSError, ValueError, RuntimeError):
                why = "couldn't read it"
            if why is None:
                if data.startswith((b"\xff\xfe", b"\xfe\xff")):
                    text = data.decode("utf-16", errors="replace")
                elif b"\0" in data[:8192]:
                    text, why = "", "it isn't text"
                else:
                    text = data.decode("utf-8-sig", errors="replace")
                if why is None:
                    text = text.replace("\r\n", "\n")
                    kind = next((k for k, pattern in _SECRETS if pattern.search(text)), None)
                    if kind:
                        why = f"it has what looks like {kind}"
                    else:
                        diff = _new_file_diff(name, text)
                        size = len(mark_hidden_characters(diff))
                        if size > room:
                            why = f"no room left: the panel takes up to {MAX_MATERIAL_CHARS:,} characters"
                        else:
                            parts.append(diff)
                            room -= size
        if why:
            left_out.append(f"{name} ({why})")
    if rest:
        left_out.append(f"{len(rest)} more, not read")
    return "".join(parts), left_out


def read_files(paths: list[str], cwd: str | os.PathLike | None = None) -> Material:
    """Files you name, read as they are on disk."""
    if len(paths) > MAX_FILES:
        raise MaterialError(f"That's {len(paths)} files; up to {MAX_FILES} at a time.")
    base = Path(cwd or os.getcwd())
    parts, names = [], []
    for given in paths:
        try:
            path = Path(given).expanduser()
            path = path if path.is_absolute() else base / path
            if not path.is_file():
                raise MaterialError(f"{given}: {'is a folder, not a file' if path.is_dir() else 'no such file'}.")
            if path.stat().st_size > MAX_MATERIAL_CHARS * 4:
                raise MaterialError(f"{given} is too big to review ({path.stat().st_size:,} bytes).")
            data = path.read_bytes()
        except (OSError, RuntimeError) as exc:  # locked, no permission, ~nosuchuser
            raise MaterialError(f"{given}: couldn't read it ({getattr(exc, 'strerror', None) or exc}).") from None
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16, as PowerShell 5.1 writes
            text = data.decode("utf-16", errors="replace")
        elif b"\0" in data[:8192]:
            raise MaterialError(f"{given} looks like a binary file, not code.")
        else:
            text = data.decode("utf-8-sig", errors="replace")
        try:
            name = str(path.resolve().relative_to(base.resolve())).replace("\\", "/")
        except (ValueError, OSError):
            name = str(given)
        names.append(name)
        parts.append(f"=== {name} ===\n{text.replace(chr(13) + chr(10), chr(10))}")
    if not parts:
        raise MaterialError("Name at least one file.")
    return Material(title="files the user chose", text="\n\n".join(parts), files=names)


def combine(*materials: Material | None) -> Material | None:
    found = [m for m in materials if m is not None]
    if len(found) <= 1:
        return found[0] if found else None
    return Material(title=" and ".join(m.title for m in found), text="\n\n".join(m.text for m in found),
                    files=[f for m in found for f in m.files], notes=[n for m in found for n in m.notes])


def code_for_review(question: str, diff: str | None = None, base: str | None = None, files: list[str] | None = None,
                    code: str | None = None, code_title: str = "code the user attached",
                    allow_secrets: bool = False, new_files: bool = False,
                    head: str | None = None) -> tuple[Material | None, str]:
    """
    What every front end does with the code a review asked for: a git diff (see git_diff), files, and/or
    code pasted in, read and checked, and the question to ask about it, which is the one given or, when
    that's blank, a default for a diff or for code. (None, question) when there's no code; MaterialError
    when there is and it can't be sent.
    """
    material = combine(git_diff(diff, base, new_files=new_files, head_ref=head) if diff else None,
                       read_files(files) if files else None,
                       pasted(code, code_title) if code and code.strip() else None)
    if material is None:
        return None, question
    check(material, allow_secrets=allow_secrets)
    if not question.strip():
        question = DEFAULT_CODE_QUESTION if diff else DEFAULT_FILES_QUESTION
    return material, question
