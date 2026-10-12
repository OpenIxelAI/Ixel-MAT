"""
One shared list of secret folders and files (keys, credentials, browser profiles and sessions, Ixel's own config), for the agent's
command sandboxes to hide: the Linux one (bubblewrap) does. The Windows one (a low-integrity token) limits
writes only and can't hide them, so a command there can read these folders.
"""
from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

from ixel_mat.config import secrets as _secrets

# Home folders and files that hold keys, logins or browser sessions, on macOS, Linux and Windows. tools.py
# refuses paths under them for Computer access, and the sandboxes hide or deny reads of them (secret_folders).
# Refuse the whole browser folder, including cookies and login databases outside its Profiles subfolder. A
# file works here as a folder does: the sandbox rules and the path check both match the file itself.
# Files named .env* are found by name (secret_folders, is_secret_name).
SECRET_HOME_FOLDERS = (
    ".ssh", ".aws", ".gnupg", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".config/gh", ".kube",
    ".docker/config.json", ".config/gcloud", ".azure",
    "Library/Keychains", "Library/Safari", "Library/Cookies",
    "Library/WebKit", "Library/Containers/com.apple.Safari",
    "Library/Group Containers/group.com.apple.Safari",
    "Library/Application Support/Google/Chrome", "Library/Application Support/Chromium",
    "Library/Application Support/Firefox", "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge", "Library/Application Support/Arc",
    "Library/Application Support/Vivaldi", "Library/Application Support/com.operasoftware.Opera",
    ".mozilla", ".config/google-chrome", ".config/chromium", ".config/BraveSoftware",
    ".config/microsoft-edge", ".config/vivaldi", ".config/opera", ".config/mozilla",
    ".var/app/org.mozilla.firefox", ".var/app/com.google.Chrome", ".var/app/org.chromium.Chromium",
    ".var/app/com.brave.Browser", "snap/firefox", "snap/chromium",
    "AppData/Local/Google/Chrome", "AppData/Local/Chromium", "AppData/Local/BraveSoftware",
    "AppData/Local/Microsoft/Edge", "AppData/Local/Vivaldi", "AppData/Local/Mozilla/Firefox",
    "AppData/Roaming/Mozilla/Firefox", "AppData/Roaming/Opera Software",
)


def _homes() -> list[Path]:
    """The home folder, and the account's own home folder too when $HOME points somewhere else."""
    homes = [Path.home()]
    if os.name == "posix":
        try:
            import pwd
            account = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):
            account = None
        if account is not None and account != homes[0]:
            homes.append(account)
    return homes


def secret_folders() -> list[str]:
    """The real paths a command must not read: the secret folders and files (SECRET_HOME_FOLDERS) under the
    home folder, and under the account's own home folder too when $HOME points somewhere else, and the
    .env* files directly in a home folder (not deeper), then Ixel's own config folder, where its keys live.
    Read each time, so tests can move the home."""
    homes = _homes()
    paths = [os.path.realpath(home / folder) for home in homes for folder in SECRET_HOME_FOLDERS]
    for home in homes:
        with suppress(OSError), os.scandir(home) as entries:
            for name in sorted(entry.name for entry in entries if entry.name.lower().startswith(".env")):
                paths.append(os.path.realpath(home / name))
    paths.append(os.path.realpath(_secrets._ENV_DIR))
    return list(dict.fromkeys(paths))
