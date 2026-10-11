import sys; sys.argv = ["x"]
from panel_harness import *
with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake:
    write_config(fake.openai_url)
    run("review-badmod", "review", "--moderator", "nobody", "x")
    run("review-json-badmod", "review", "--json", "--moderator", "nobody", "x")
    run("review-timeout0", "review", "--timeout", "0", "x")
    run("review-json-timeout0", "review", "--json", "--timeout", "0", "x")
    run("review-mode-bogus", "review", "--mode", "bogus", "x")
    run("review-newfiles-alone", "review", "--new-files", "x")
    run("review-file-missing", "review", "-f", "nope.py", "x")
    run("review-json-file-missing", "review", "--json", "-f", "nope.py", "x")
    run("ask-json-file-missing", "ask", "--json", "--agent", "gpt", "-f", "nope.py", "x")
    run("ask-noconn", "ask", "--agent", "gpt", "x", IXEL_TEST_PANEL_KEY="")
    run("review-nokey", "review", "x", IXEL_TEST_PANEL_KEY="")
    run("review-json-nokey", "review", "--json", "x", IXEL_TEST_PANEL_KEY="")
