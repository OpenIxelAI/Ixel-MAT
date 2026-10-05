"""
The Board's pull requests (see connections.py), for the page: listed for a project, the host set up (what
kind it is, its address, a token), and one fetched into the project and reviewed through Handoff: a task
is added, approved for exactly the commits fetched (sealed, like any approval), and run.

Everything here is blocking (git, the host's API, Handoff): the server runs it in a thread.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ixel_mat import connections
from ixel_mat.config import edit, secrets
from ixel_mat.connections import ConnectionError_
from ixel_mat.gui import handoff_api, settings_api


class ConnectionsApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _problem(exc: ConnectionError_) -> dict:
    return {"code": exc.code, "message": str(exc)}


def _suggested_web(origin: connections.Origin) -> str:
    return f"{origin.scheme}://{connections._netloc(origin.host, origin.port)}"


def look(settings, project: Any) -> dict:
    """{origin, host, prs} for a project, or with `problem` ({code, message}) saying what's needed first."""
    root = handoff_api.check_project(project)
    try:
        origin = connections.project_origin(root)
    except ConnectionError_ as exc:
        return {"problem": _problem(exc)}
    out: dict = {"origin": {"host": connections.setting_name(origin), "path": origin.path,
                            "web": _suggested_web(origin)}}
    try:
        host = connections.host_for(origin, settings.config)
    except ConnectionError_ as exc:
        return {**out, "problem": _problem(exc)}
    section = settings.config.get("connections")
    out["host"] = {"kind": host.kind, "label": host.label, "web": host.web, "token": secrets.key_state(host.token_env),
                   "set": isinstance(section, dict) and isinstance(section.get(connections.setting_name(origin)), dict)}
    try:
        out["prs"] = connections.pull_requests(origin, host, connections.token_for(host))
    except ConnectionError_ as exc:
        out["problem"] = _problem(exc)
    return out


def _origin_and_path(settings, project: Any) -> tuple[Path, connections.Origin]:
    root = handoff_api.check_project(project)
    try:
        return root, connections.project_origin(root)
    except ConnectionError_ as exc:
        raise ConnectionsApiError(str(exc)) from None


def set_host(settings, body: Any) -> dict:
    """What kind of host the project's origin is on, and its web address: [connections."host"]."""
    if not isinstance(body, dict):
        raise ConnectionsApiError("Expected a JSON object.")
    _, origin = _origin_and_path(settings, body.get("project"))
    kind = body.get("kind")
    if kind not in connections.KINDS:
        raise ConnectionsApiError("Say whether it's Gitea (or Forgejo), GitLab or GitHub.")
    name = connections.setting_name(origin)
    try:
        web = connections.check_web(body.get("url") if isinstance(body.get("url"), str) else "")
        connections.host_for(origin, {"connections": {name: {"kind": kind, "url": web}}})  # the same host and port
    except ConnectionError_ as exc:
        raise ConnectionsApiError(f"This project's origin is on {name}, so its address has to be too."
                                  if exc.code == "invalid" and "somewhere else" in str(exc) else str(exc)) from None
    path = settings_api.settings_path(settings)
    if path is None or settings.config.get("_error"):
        raise ConnectionsApiError("There's no settings file to save this in. Run ixel setup first.", 409)
    try:
        edit.edit_file(path, ("connections", name), {"kind": kind, "url": web},
                       backup=not settings_api._holds_secrets(settings.config))
    except edit.EditError as exc:
        raise ConnectionsApiError(f"Ixel didn't change the file: {exc}. Change it by hand in {path}.", 422) from None
    return {"saved": True}


def set_token(settings, body: Any) -> dict:
    """Save or remove the token for the project's host (write-only: it's never sent back)."""
    if not isinstance(body, dict):
        raise ConnectionsApiError("Expected a JSON object.")
    _, origin = _origin_and_path(settings, body.get("project"))
    try:
        host = connections.host_for(origin, settings.config)
    except ConnectionError_ as exc:
        raise ConnectionsApiError(str(exc)) from None
    name = urlparse(host.web).netloc
    if body.get("remove") is True:
        try:
            secrets.remove_live(host.token_env)
        except secrets.KeyStoreError as exc:
            raise ConnectionsApiError(str(exc), 503) from None
        state = secrets.key_state(host.token_env)
        return {"state": state, "message": f"The token for {name} is also set outside Ixel ({host.token_env}), and "
                                           "that one is still used." if state == "system" else
                f"Removed the token for {name}."}
    if not connections.token_allowed(host.web):
        raise ConnectionsApiError(connections.PLAIN_HTTP.format(host=name) + " A public repository needs no "
                                  "token.", 409)
    try:
        value = settings_api.check_key_value(body.get("value"))
    except settings_api.SettingsError as exc:
        raise ConnectionsApiError(str(exc).replace("key", "token")) from None
    try:
        outcome = secrets.set_live(host.token_env, value)
    except secrets.KeyStoreError as exc:
        raise ConnectionsApiError(str(exc), 503) from None
    if outcome == "system":
        return {"state": "system", "message": f"Saved, but {host.token_env} is also set outside Ixel, and that one "
                                              "wins. Change or remove it there."}
    return {"state": "file", "message": f"Saved the token for {name}. It's only ever sent to {name}."}


def _label(kind: str, pr: dict) -> str:
    what = f"merge request !{pr['number']}" if kind == "gitlab" else f"pull request #{pr['number']}"
    branches = f"{pr['head']} into {pr['base']}" if pr["head"] else f"into {pr['base']}"
    return f"{what} ({branches})"[:300]


MAX_FIX_BYTES = 16_000  # what to change, in UTF-8: within a Handoff task's body (20 KB) with the rest
_SHELL_SAFE = re.compile(r"^[\w./+-]+$")  # a branch name that means the same pasted into any shell


@dataclass
class Plan:
    """A pull request fetched into the project, and the Handoff task that will review or change it."""
    root: Path
    kind: str           # review | edit
    agent: str
    label: str
    title: str
    body: str
    target: dict
    branch: str         # the PR's branch, when pushing to origin updates it ("" from a fork)


def _fetched(settings, body: Any) -> tuple[Path, connections.Host, dict, dict, str, str]:
    """One open PR named in the request, fetched into the project → (root, host, pr, fetched, label, note)."""
    if not isinstance(body, dict):
        raise ConnectionsApiError("Expected a JSON object.")
    root, origin = _origin_and_path(settings, body.get("project"))
    number, agent = body.get("number"), body.get("agent")
    if not isinstance(number, int) or isinstance(number, bool) or not isinstance(agent, str) or not agent:
        raise ConnectionsApiError("Say which pull request, and who works on it.")
    try:
        host = connections.host_for(origin, settings.config)
        listed = connections.pull_requests(origin, host, connections.token_for(host))
        pr = next((p for p in listed if p["number"] == number), None)
        if pr is None:
            raise ConnectionsApiError(f"#{number} isn't one of the open pull requests on {origin.host} any more.", 404)
        if not pr["base"]:
            raise ConnectionsApiError(f"{host.label} didn't say which branch #{number} goes into, or it's one "
                                      "Ixel can't fetch.")
        fetched = connections.fetch(root, host.kind, number, pr["base"])
    except ConnectionError_ as exc:
        raise ConnectionsApiError(str(exc), 502 if exc.code == "unreachable" else 400) from None
    note = (f"Fetched into {fetched['head_ref']} (its base, {pr['base']}, into {fetched['base_ref']}); your own files "
            "and branches weren't changed." + (f"\n{pr['url']}" if pr["url"] else ""))
    if pr["head_sha"] and pr["head_sha"] != fetched["head"]:
        note += "\nIt changed while it was being fetched: this is what git fetched."
    return root, host, pr, fetched, _label(host.kind, pr), note


def plan_review(settings, body: Any) -> Plan:
    """Fetch one open PR into the project, for `agent` to review exactly its commits."""
    root, _, pr, fetched, label, note = _fetched(settings, body)
    return Plan(root, "review", body["agent"], label, f"Review {label}: {pr['title']}"[:200].rstrip(),
                f"By {pr['author'] or 'someone'}.\n{note}",
                {"base": fetched["base"], "head": fetched["head"], "label": label}, "")


def plan_fix(settings, body: Any) -> Plan:
    """Fetch one open PR, for `agent` (Claude or Codex) to change as `text` says, on a branch of its own that
    starts from the PR's last commit."""
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ConnectionsApiError("Say what to change.")
    if len(text.encode("utf-8")) > MAX_FIX_BYTES:
        raise ConnectionsApiError(f"That's more than {MAX_FIX_BYTES // 1000} KB of text. Shorten it to what needs "
                                  "changing.")
    root, _, pr, fetched, label, note = _fetched(settings, body)
    return Plan(root, "edit", body["agent"], label, f"Fix {label}: {pr['title']}"[:200].rstrip(),
                f"{text.strip()}\n\n(The pull request is by {pr['author'] or 'someone'}. {note})",
                {"base": fetched["base"], "head": fetched["head"], "label": label},
                pr["head"] if pr["head"] and not pr["fork"] else "")


def _push_note(plan: Plan, ref: str) -> tuple[str, str]:
    """The line that adds a fix to its PR, and what the task says about it."""
    if not plan.branch:
        return "", (f"This pull request's branch is in another repository (a fork), so pushing to origin won't "
                    f"update it. When the run is done, the work is on handoff/{ref}: push it to that fork, or open "
                    "a pull request from it.")
    if not _SHELL_SAFE.fullmatch(plan.branch) or plan.branch.startswith("refs/"):
        return "", (f"When the run is done, the work is on handoff/{ref}. The pull request's branch name has "
                    "characters a shell would read as commands, so Ixel doesn't write out a line to paste: push "
                    f"handoff/{ref} to that branch yourself.")
    push = f"git push origin handoff/{ref}:refs/heads/{plan.branch}"  # (a branch, never a tag of that name)
    return push, (f"When the run is done, the work is on handoff/{ref}, starting from the pull request's last "
                  f"commit. To add it to the pull request: {push}")


def start(plan: Plan, handoff: Callable[[str, Path | None, dict], dict]) -> dict:
    """Add the task, approve it for exactly the fetched commits, and run it. `handoff(op, root, args)` calls
    Handoff's api. → {"task", "label"}, with "push" for a fix and "problem" when it couldn't start."""
    if "run-target" not in (handoff("hello", None, {}).get("features") or []):
        raise ConnectionsApiError("This Handoff can't review or change a pull request yet. Update it with: "
                                  "handoff update", 409)
    ref = handoff("add", plan.root, {"title": plan.title, "body": plan.body})["task"]["ref"]
    out: dict = {"task": ref, "label": plan.label}
    try:
        if plan.kind == "edit":
            out["push"], said = _push_note(plan, ref)
            handoff("note", plan.root, {"task": ref, "text": said})
        approved = handoff("approve", plan.root, {"task": ref, "agent": plan.agent, "kind": plan.kind,
                                                  "target": plan.target})
        if ((approved.get("task") or {}).get("run") or {}).get("of") != plan.label:
            raise ConnectionsApiError("Handoff didn't approve it for the pull request's commits. Update it with: "
                                      "handoff update", 409)
    except (handoff_api.HandoffApiError, ConnectionsApiError) as exc:
        try:  # nothing half-made left behind, so trying again doesn't add it twice
            handoff("delete", plan.root, {"task": ref})
        except handoff_api.HandoffApiError:
            raise ConnectionsApiError(f"{exc} ({ref} was added, and is still on the board.)",
                                      getattr(exc, "status", 409)) from None
        raise ConnectionsApiError(str(exc), getattr(exc, "status", 409)) from None
    try:
        handoff("run.start", plan.root, {"task": ref})
    except handoff_api.HandoffApiError as exc:  # it's on the board, approved: the page says why it didn't start
        out["problem"] = str(exc)
    return out
