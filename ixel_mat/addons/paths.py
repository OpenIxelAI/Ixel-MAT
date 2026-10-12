"""
Where add-ons live: their folders in ~/.ixel/addons (with state.json beside them), the
diagrams Diagram Design makes in ~/.ixel/diagrams, and the built-in cards, <id>.toml,
beside this file. Every module reads these at call time as paths.X, never copying them
with `from ... import`, so tests can monkeypatch them here.
"""
from pathlib import Path

ADDONS_DIR = Path.home() / ".ixel" / "addons"
DIAGRAMS_DIR = Path.home() / ".ixel" / "diagrams"
BUILTIN_DIR = Path(__file__).resolve().parent  # built-in cards, <id>.toml, sit beside this file


def state_path() -> Path:
    return ADDONS_DIR / "state.json"
