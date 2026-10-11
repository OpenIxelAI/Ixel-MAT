import sys
sys.path.insert(0, "/home/user/Ixel-MAT")
from ixel_mat import mcp_server as m
from pathlib import Path
# Windows, python in a path with spaces, inside a venv
m.WINDOWS = True
sys.executable = r"C:\Users\Jane Doe\AppData\Local\Programs\ixel\.venv\Scripts\python.exe"
m._safe_path_flags = lambda: ["-I"]
m.windows_desktop_config = lambda: r"%APPDATA%\Claude\claude_desktop_config.json"
print(m.host_snippets())
print("=================== non-Windows, ixel beside python, space in path")
m.WINDOWS = False
import os
d = "/tmp/claude-0/-home-user-Ixel-MAT/c585261b-b9d6-5506-884a-19d4ae6ba125/scratchpad/area-mcp-machines/venv dir/bin"
os.makedirs(d, exist_ok=True); Path(d, "ixel").touch(); Path(d, "python").touch()
sys.executable = d + "/python"
print(m.host_snippets()[:700])
