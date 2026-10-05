"""
What Ixel keeps of what you asked and ran, and forgetting it.

- conversation.json: your last few `ixel review` questions and answers, for --continue. Each one goes
  once it's a day old.
- machines.log: what Machines did (never a command's text). Lines go once they're 30 days old.
- The app window's browser storage (`ixel app`): what the page keeps there, such as the project folders
  you used last, and the browser's own cache.

tidy() runs as every ixel command starts and deletes what's past its time. forget() deletes the files at
once (`ixel forget`, and the Forget button on the app's Settings page, which leaves the window's storage
alone since it's in use). Your keys, settings, machines and usage stats (stats.json) are never touched.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ixel_mat import conversation
from ixel_mat.machines import log

CONVERSATION = "your last ixel review conversation"
WINDOW = "the app window's storage"
# A window folder that was a link to one somewhere else: what it points to could be anything
LINKED = "it was a link to {}. Ixel removed the link and left that folder as it was"


@dataclass(frozen=True)
class Forgotten:
    """One thing forget() found: what it is, for a person ("the Machines log"), where, and "" once it's
    deleted or why it couldn't be."""
    what: str
    path: Path
    error: str = ""

    def to_dict(self) -> dict:
        return {"what": self.what, "path": str(self.path), "error": self.error}


def tidy() -> None:
    """Drops each review exchange once it's a day old, and log lines once they're 30 days old. A stat or two,
    and a rewrite or a delete only when something is due. Never raises: a file it can't reach waits for the
    next time."""
    try:
        conversation.expire()
        log.tidy()
    except Exception:  # noqa: BLE001 — never in the way of the command that's starting
        pass


def window_folders(system: str = sys.platform, env: Mapping[str, str] = os.environ,
                   home: Path | None = None) -> list[Path]:
    """Where the app's window keeps its browser storage: the Edge or Chrome profile `ixel app` opens, and
    on a Mac, Ixel.app's own (WebKit keeps it under the app's name)."""
    from ixel_mat.gui.window import MAC_APP_ID, data_dir

    home = home or Path.home()
    folders = [data_dir(system, env, home) / "window"]
    if system == "darwin":
        folders.append(home / "Library" / "WebKit" / MAC_APP_ID)
    return folders


def forget(window: bool = True) -> list[Forgotten]:
    """Deletes the conversation and the Machines log, and with window, the window's browser storage.
    Returns what was there: what's not there isn't listed."""
    found = []
    try:
        conversation.CONVERSATION_FILE.unlink()
        found.append(Forgotten(CONVERSATION, conversation.CONVERSATION_FILE))
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError as exc:
        found.append(Forgotten(CONVERSATION, conversation.CONVERSATION_FILE, exc.strerror or str(exc)))
    found += [Forgotten("the Machines log", path, error) for path, error in log.delete()]
    if window:
        for folder in window_folders():
            error = _remove_folder(folder)
            if error is not None:
                found.append(Forgotten(WINDOW, folder, error))
    return found


def _remove_folder(folder: Path) -> str | None:
    """Deletes a folder with everything in it: "" once it's gone, why not, or None if it wasn't there.
    It's renamed first. On Windows that's refused while a window has files in it open, so a folder in
    use is left whole, never half deleted. A link to a folder somewhere else goes, and what it points to
    stays (LINKED says where that is)."""
    aside = folder.with_name(folder.name + ".forgotten")
    if not os.path.lexists(folder) and not os.path.lexists(aside):
        return None
    linked = ""
    try:
        if _is_link(folder):
            linked = LINKED.format(os.path.realpath(folder))
            folder.unlink()
        if _is_link(aside):  # Ixel's name for a folder on its way out: a link there goes, what it points to stays
            aside.unlink()
        elif aside.exists():  # left from a time it couldn't all be deleted
            _rmtree(aside)
        if folder.exists():
            folder.rename(aside)
            _rmtree(aside)
    except OSError as exc:
        return exc.strerror or str(exc)
    return linked


def _is_link(path: Path) -> bool:
    """A symbolic link, or on Windows a junction, which points to a folder elsewhere just the same
    (os.path.isjunction is Python 3.12's; this is how shutil.rmtree tells)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    reparse_point = getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_point) and getattr(st, "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)


def _rmtree(folder: Path) -> None:
    """shutil.rmtree, read-only files included (Windows won't delete one until its flag is cleared). It
    never goes through a link: rmtree removes a link inside the folder, not what it points to, and the
    flag is cleared only on a file or folder that's in it, never through a link (chmod would follow one).
    Anything else that goes wrong is raised."""
    def writable_again(func, path, error):
        if func not in (os.unlink, os.remove, os.rmdir) or _is_link(Path(path)):
            raise error if isinstance(error, BaseException) else error[1]
        os.chmod(path, stat.S_IWRITE)
        func(path)
    if sys.version_info >= (3, 12):
        shutil.rmtree(folder, onexc=writable_again)
    else:
        shutil.rmtree(folder, onerror=writable_again)
