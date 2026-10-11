import sys
sys.path.insert(0, "/home/user/Ixel-MAT")
from pathlib import Path
from ixel_mat import connections as C
root = Path(sys.argv[1])
print(C.fetch(root, "github", 1, "main"))
marker = root.parent / "hookmarker"
print("hook marker:", marker.read_text() if marker.exists() else "(hooks did not run)")
