import sys; sys.argv = ["x"]
from panel_harness import *
with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake:
    write_config(fake.openai_url)
    big = "x" * 2100
    p = run("repl-piped-bigpaste", "", stdin=big + "\nWhat is 17 × 23?\n/quit\n")
    out = p.stdout
    print("\nSUMMARY: 'Large paste detected' in out:", "Large paste detected" in out)
    print("SUMMARY: 'Please enter Y or N' in out:", "Please enter Y or N" in out)
    print("SUMMARY: reviews run (count of 'Round 1/'):", out.count("Round 1/"))
    print("SUMMARY: second question asked:", "What is 17 × 23?" in out)
