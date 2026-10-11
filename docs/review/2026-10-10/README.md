# Ixel MAT review, 2026-10-10

`review-and-fix-plan.md` is the review and the hand-off plan (ten work packages). `area-reports/` are the six
area reviewers' reports in compressed form. `probes/` are the scripts that reproduce the findings; they were
written against the repository at commit b9024e3 and refer to a scratch folder under /tmp that no longer
exists, so run them from a checkout with `IXEL_TEST_KEYCHAIN=memory IXEL_NO_UPDATE_CHECK=1 python -m pytest -q
<file>` (or `python <file>`) and fix the paths as needed; they are seeds for regression tests, not tests.
`screenshots/` are the browser app as a new user sees it and after real reviews against the fake providers.

Done so far: WP1 (PR #7) and WP2 (PR #8). Next: WP3, honest costs and robust stats.
