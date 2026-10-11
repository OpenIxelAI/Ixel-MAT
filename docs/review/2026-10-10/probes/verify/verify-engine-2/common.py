import io, sys
sys.path.insert(0, "/home/user/Ixel-MAT/tests")
from rich.console import Console
from ixel_mat.review_ui import report

def render(result, width=110):
    buf = io.StringIO()
    con = Console(file=buf, width=width, force_terminal=False, color_system=None)
    for r in report(result):
        con.print(r)
    return buf.getvalue()
