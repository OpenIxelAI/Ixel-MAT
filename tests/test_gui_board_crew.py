"""The app's Board asks Handoff to pick the crew for a task (its "auto-crew" feature), for the Run form's Auto."""
from test_gui_board import fake, get, gui_with, project, q  # noqa: F401
from test_gui_server import AUTH, run_with_client

CREW = {"job": "coding", "size": "heavy", "how": "keywords",
        "chain": ["claude-opus-5-5", "grok:grok-4.7", "claude-haiku-5-5"],
        "efforts": {"claude-opus-5-5": "xhigh", "claude-haiku-5-5": "low"},
        "roles": [{"role": "plans", "model": "claude-opus-5-5", "effort": "xhigh", "why": "It plans the heavy part"},
                  {"role": "builds", "model": "grok:grok-4.7", "effort": "", "why": "It writes the code"},
                  {"role": "small steps", "model": "claude-haiku-5-5", "effort": "low", "why": "Tests and docs"}]}


def with_feature(fake):  # noqa: F811
    fake.answer("hello", {"data": {"handoff_version": "0.9.0", "schema": 1, "ops": ["board", "crew"],
                                   "features": ["auto-crew"]}})


def crew_calls(fake):  # noqa: F811
    return [c["request"] for c in fake.calls() if c["request"]["op"] == "crew"]


def test_the_board_asks_handoff_for_the_crew_and_cleans_the_reply(project, fake):  # noqa: F811
    with_feature(fake)
    messy_roles = [{**CREW["roles"][0], "secret": "x"}, "nope", {"role": "builds", "why": 3}]
    fake.answer("crew", {"data": {**CREW, "chain": [*CREW["chain"], 7], "roles": messy_roles,
                                  "efforts": {"claude-opus-5-5": "xhigh", "claude-haiku-5-5": 4}}})
    [(status, crew)] = get(gui_with(fake.command), f"/api/board/crew?project={q(project)}&task=T-3&effort_all=max")
    assert status == 200
    assert crew["job"] == "coding" and crew["size"] == "heavy" and crew["how"] == "keywords"
    assert crew["chain"] == ["claude-opus-5-5", "grok:grok-4.7", "claude-haiku-5-5"]  # the number is dropped
    assert crew["efforts"] == {"claude-opus-5-5": "xhigh"}
    assert crew["roles"] == [CREW["roles"][0], {"role": "builds", "model": "", "effort": "", "why": ""}]
    [call] = crew_calls(fake)
    assert call["args"] == {"task": "T-3", "effort_all": "max"} and call["project"] == str(project)


def test_the_board_can_ask_for_a_crew_for_some_text(project, fake):  # noqa: F811
    with_feature(fake)
    fake.answer("crew", {"data": CREW})
    [(status, crew)] = get(gui_with(fake.command), f"/api/board/crew?project={q(project)}&text={q('  Fix the login ')}")
    assert status == 200 and crew["roles"][1]["model"] == "grok:grok-4.7"
    [call] = crew_calls(fake)
    assert call["args"] == {"text": "Fix the login"}


def test_a_handoff_without_auto_crew_gets_no_question(project, fake):  # noqa: F811
    [(status, body)] = get(gui_with(fake.command), f"/api/board/crew?project={q(project)}&task=T-3")  # no features
    assert status == 404 and body["code"] == "no_auto_crew" and "handoff update" in body["error"]
    assert crew_calls(fake) == []
    fake.answer("hello", {"data": {"handoff_version": "0.9.0", "schema": 1, "ops": ["board"], "features": ["other"]}})
    [(status, body)] = get(gui_with(fake.command), f"/api/board/crew?project={q(project)}&task=T-3")
    assert status == 404 and body["code"] == "no_auto_crew" and crew_calls(fake) == []


def test_a_crew_question_that_makes_no_sense_is_refused(project, fake):  # noqa: F811
    with_feature(fake)
    base = f"/api/board/crew?project={q(project)}"
    answers = get(gui_with(fake.command), f"{base}&task=T-3&text=hello", base, f"{base}&task=x",
                  f"{base}&task=T-3&effort_all={q('MAX!')}", f"{base}&task=T-1234567")
    assert [(status, body["code"]) for status, body in answers] == [(400, "invalid")] * 5
    assert crew_calls(fake) == []


def test_a_handoff_error_for_the_crew_keeps_its_code(project, fake):  # noqa: F811
    with_feature(fake)
    fake.answer("crew", {"error": {"code": "not_found", "message": "no such task"}})
    [(status, body)] = get(gui_with(fake.command), f"/api/board/crew?project={q(project)}&task=T-9")
    assert status == 404 and body["code"] == "not_found" and "no such task" in body["error"]
    assert len(crew_calls(fake)) == 1


def test_the_hello_lists_what_handoff_can_do(project, fake):  # noqa: F811
    fake.answer("hello", {"data": {"handoff_version": "0.9.0", "schema": 1, "ops": ["board", "crew"],
                                   "features": ["auto-crew", 3]}})
    [(status, hello)] = get(gui_with(fake.command), "/api/board/hello")
    assert status == 200 and hello["ok"] is True and hello["features"] == ["auto-crew"]


def test_an_updated_handoff_is_noticed_without_a_restart(project, fake):  # noqa: F811
    fake.answer("hello", {"data": {"handoff_version": "0.9.0", "schema": 1, "ops": ["board"], "features": ["other"]}})
    fake.answer("crew", {"data": CREW})
    path = f"/api/board/crew?project={q(project)}&task=T-3"

    async def scenario(client):
        before = await client.get(path, headers=AUTH)
        missing = (before.status, await before.json())
        with_feature(fake)  # the person runs `handoff update` between the two requests
        after = await client.get(path, headers=AUTH)
        return missing, (after.status, await after.json())

    (missing_status, missing), (status, crew) = run_with_client(gui_with(fake.command), scenario)
    assert missing_status == 404 and missing["code"] == "no_auto_crew"
    assert status == 200 and crew["job"] == "coding"
    assert len(crew_calls(fake)) == 1


def test_the_hello_is_asked_again_only_while_auto_crew_is_missing(project, fake):  # noqa: F811
    with_feature(fake)
    fake.answer("crew", {"data": CREW})
    path = f"/api/board/crew?project={q(project)}&task=T-3"
    [(status, _), (again, _)] = get(gui_with(fake.command), path, path)
    assert (status, again) == (200, 200)
    assert len([c for c in fake.calls() if c["request"]["op"] == "hello"]) == 1  # has auto-crew: asked once

    fake.answer("hello", {"data": {"handoff_version": "0.9.0", "schema": 1, "ops": ["board"], "features": ["other"]}})
    asked = len([c for c in fake.calls() if c["request"]["op"] == "hello"])
    [(status, body)] = get(gui_with(fake.command), path)
    assert status == 404 and body["code"] == "no_auto_crew"
    assert len([c for c in fake.calls() if c["request"]["op"] == "hello"]) - asked == 2  # asked, then asked again


def test_a_handoff_that_is_not_there_says_so_not_update(project):  # noqa: F811
    [(status, body)] = get(gui_with(None), f"/api/board/crew?project={q(project)}&task=T-3")
    assert status == 404 and body["code"] == "not_installed" and "handoff update" not in body["error"]


def test_a_hello_that_isnt_ok_gives_its_own_problem_not_no_auto_crew(project, fake):  # noqa: F811
    path = f"/api/board/crew?project={q(project)}&task=T-3"
    fake.answer("hello", {"stderr": "boom", "exit": 1})
    [(status, body)] = get(gui_with(fake.command), path)
    assert status == 502 and body["code"] == "internal" and body["error"] == "boom"
    assert crew_calls(fake) == []

    fake.answer("hello", {"data": {}})  # a hello with no version it can use: outdated, with the update text
    [(status, body)] = get(gui_with(fake.command), path)
    assert status == 404 and body["code"] == "outdated" and "handoff update" in body["error"]

    fake.answer("hello", {"data": {}, "schema": 2})  # a newer envelope: outdated too, from handoff_api's message
    [(status, body)] = get(gui_with(fake.command), path)
    assert status == 404 and body["code"] == "outdated"
    assert crew_calls(fake) == []


def test_text_longer_than_4000_characters_is_refused_not_cut(project, fake):  # noqa: F811
    with_feature(fake)
    fake.answer("crew", {"data": CREW})
    base = f"/api/board/crew?project={q(project)}"
    [(long_status, long_body), (ok_status, crew)] = get(gui_with(fake.command), f"{base}&text={'a' * 4001}",
                                                       f"{base}&text={'a' * 4000}")
    assert (long_status, long_body["code"]) == (400, "invalid")
    assert ok_status == 200 and crew["job"] == "coding"
    [call] = crew_calls(fake)  # only the 4000 characters went to Handoff
    assert len(call["args"]["text"]) == 4000


def test_efforts_keys_are_dropped_not_cut_and_capped(project, fake):  # noqa: F811
    with_feature(fake)
    efforts = {"m" * 80: "high", "a" * 80 + "x": "low", "a" * 80 + "y": "low", "m" * 81: "low",
               **{f"model-{i}": "low" for i in range(8)}}
    fake.answer("crew", {"data": {**CREW, "efforts": efforts}})
    [(status, crew)] = get(gui_with(fake.command), f"/api/board/crew?project={q(project)}&task=T-3")
    assert status == 200
    keys = list(crew["efforts"])
    assert len(keys) == 6 and crew["efforts"]["m" * 80] == "high"  # the 80-character key is kept, whole
    assert all(len(k) <= 80 for k in keys)
    assert "a" * 80 not in keys and not any(k.startswith("a") for k in keys)  # neither long key survives cut
