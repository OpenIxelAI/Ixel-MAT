import sys
sys.stderr.write("PLANTED subprocess.py IMPORTED from cwd\n")
class Popen:
    def __init__(self, *a, **k): pass
    def wait(self): return 99
