# Engine (modes/review.py, runtime, triage, usage, stats, material, models, effort, sanitize)
B1 high CONFIRMED review.py:711-746 _Run.calls: failed tasks count as "done"; two instant failures (401, CLI missing) -> took≈0 -> third healthy model cut off at SLOWEST_WAIT_MIN=30s -> "No agent produced an answer". Fix: measure from replies, no grace until one reply; if all others failed, let last run to its limit. Same in review/revise/fix rounds.
B2 med CONFIRMED review.py:1082-1113 _verify: parsed-but-incomplete JSON ({"status":"confirmed"} w/o "use" — natural with a single draft; send_back with []) becomes the final answer text, verifier_outcome="corrected"; stats counts big_model_answers and resets saver streak (stats.py:206-208).
B3 med CONFIRMED review.py:505-530 _parse_verdict: notes block first / empty pre-marker text -> whole raw incl. <IXEL-… notes> fence tags shown as the answer, confidence "uncertain".
B4 med CONFIRMED review.py:784-792,906-915,958-959 deep mode: moderator prompt tags revised answers with old peer score 0.00 and old flagged errors.
B5 low-med CONFIRMED review.py:99-109,490-493: "incorrect." / "Correct!" -> unsure; "A." / "(A)" -> dropped.
B6 med CONFIRMED review.py:681 + usage.py:300-319, http.py:324-330: cancelled/timed-out API calls absent from cost; no "N calls not counted".
B7 low-med CONFIRMED review.py:213 standings: all-unsure answer sorts below unanimous-incorrect; standings()[0] is fallback answer (977, 1116).
B8 low CONFIRMED review.py:246-253 agreement() "strong" when every grade unsure.
B9 low CONFIRMED review.py:826-840 reviewer JSON naming no valid answers -> silently nothing, no agent_failed.
B10 low CONFIRMED stats.py:155-160,282-292 month total omits ≈ for estimated.
B11 low CONFIRMED stats.py:198 record_run raises TypeError on hand-edited by_model.
B12 low-med CONFIRMED-by-reading modes/full.py:55-134, mat.py _compare: /compare spend never recorded.
Plausible: Confidence.from_string substring HIGH-first ("not high" -> HIGH) schema/response.py:24-29; [review] agents=[typos] -> [] -> everyone (runtime.py).
UX: 1 failures explained in result when answers empty; 2 never show JSON/fence as answer; 3 quick-mode confidence is unchecked say-so (review.py:966) — label; 4 note for "uncertain" when moderator omitted notes; 5 _pick_moderator (897-904) picks a moderator that already failed -> second full timeout; 6 round counter jumps (verify 3/3 then fix 4/5); 7 one honest cost line (B6, B10, B12, triage not costed); 8 stream answers not just verdict; 9 scoreboard shows reviewers that graded nothing; 10 "strong agreement" needs grades.
Probes: scratchpad/area-engine/test_engine_probe.py (12 probes), probe_stats.py
