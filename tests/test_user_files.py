"""The suite never writes the files of whoever runs it: conftest.py points them at each test's own folder."""
import importlib
import pkgutil
import sys
from pathlib import Path

import ixel_mat
from ixel_mat import stats, update

# As the modules found them, before any test (or conftest's fixture) ran
REAL_STATS_FILE = stats.STATS_FILE
REAL_INSTALL_INFO = update.INSTALL_INFO
REAL_FOLDER = Path.home() / ".config" / "ixel-mat"


def test_the_real_stats_file_is_out_of_reach(tmp_path):
    # In-process reviews used to add themselves to the stats.json of whoever ran the suite
    assert REAL_STATS_FILE == REAL_FOLDER / "stats.json"
    assert stats.STATS_FILE != REAL_STATS_FILE and stats.STATS_FILE.is_relative_to(tmp_path)
    assert update.INSTALL_INFO != REAL_INSTALL_INFO and update.INSTALL_INFO.is_relative_to(tmp_path)


def test_no_module_keeps_a_path_into_your_ixel_folder(tmp_path):
    # A new file in ~/.config/ixel-mat fails this until conftest.py moves it too
    for module in pkgutil.walk_packages(ixel_mat.__path__, "ixel_mat."):
        if not module.name.endswith("__main__"):  # that one runs the app
            importlib.import_module(module.name)
    reachable = [f"{name}.{attr} = {value}" for name, module in sorted(sys.modules.items())
                 if name == "ixel_mat" or name.startswith("ixel_mat.")
                 for attr, value in vars(module).items()
                 if isinstance(value, Path) and value.is_absolute() and value.is_relative_to(REAL_FOLDER)]
    assert not reachable, "\n".join(reachable)
    assert stats.STATS_FILE.is_relative_to(tmp_path)
