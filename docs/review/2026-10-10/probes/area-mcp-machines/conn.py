import sys
sys.path.insert(0, "/home/user/Ixel-MAT")
from ixel_mat import connections as C
for u in ["git@github.com:a/b.git", "https://github.com/a/b", "https://user:tok@github.com/a/b.git", "ssh://git@github.com:2222/a/b.git",
          "https://git.example.com:8443/g/sub/r.git", "git@-evil.com:a/b", "https://github.com/a/b/c", "file:///tmp/x", "/home/u/repo", "https://gıthub.com/a/b", "https://GitHub.com/A/B"]:
    print(f"{u!r:55} -> {C.parse_remote(u)}")
print("refs:")
for r in ["main", "feat/x;rm -rf ~", "a`id`b", "$(touch /tmp/pwn)", "x|y&z", "ün-brañch", "-flag", "refs/heads/x", "a..b", "a b", "@", "x@{1}", "ok+plus", "ok'quote", 'ok"dq', "a\\b", "a*", "a[0]", "x.lock", "x.lock/y", "a/./b", "a//b", ".hidden", "ends.", "ends/"]:
    print(f"  {r!r:25} -> {C._ref(r)!r}")
print("label:", C.__name__)
from ixel_mat.gui import connections_api as A
print(A._label("github", {"number": 7, "head": "feat;rm -rf ~`id`", "base": "main"}))
print(A._push_note(A.Plan(None, "edit", "claude", "l", "t", "b", {}, "feat;rm -rf ~"), "T-1"))
print(A._push_note(A.Plan(None, "edit", "claude", "l", "t", "b", {}, "feat/ok+1"), "T-1"))
print(C.token_env("https://git.example.com"), C.token_env("https://git-example.com"))
print(C.check_web("https://Git.Example.com:8443/"))
try: print(C.check_web("https://example.com/gitea"))
except C.ConnectionError_ as e: print("subpath:", e)
