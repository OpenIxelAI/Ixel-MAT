"""
One shared list of secret folders (keys, browser profiles and sessions, Ixel's own config), for the agent's
command sandboxes to hide: the Linux one (bubblewrap) does. The Windows one (a low-integrity token) limits
writes only and can't hide them, so a command there can read these folders.
"""
from __future__ import annotations

import os
from pathlib import Path

from ixel_mat.config import secrets as _secrets

SECRET_HOME_FOLDERS = (
    ".ssh", ".aws", ".gnupg", "Library/Keychains", "Library/Safari", "Library/Cookies",
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


def secret_folders() -> list[str]:
    """The real paths a command must not read: the secret and browser folders (SECRET_HOME_FOLDERS) under the
    home folder, and under the account's own home folder too when $HOME points somewhere else, then Ixel's
    own config folder, where its keys live. Read each time, so tests can move the home."""
    homes = [Path.home()]
    if os.name == "posix":
        try:
            import pwd
            account = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):
            account = None
        if account is not None and account != homes[0]:
            homes.append(account)
    paths = [os.path.realpath(home / folder) for home in homes for folder in SECRET_HOME_FOLDERS]
    paths.append(os.path.realpath(_secrets._ENV_DIR))
    return list(dict.fromkeys(paths))
