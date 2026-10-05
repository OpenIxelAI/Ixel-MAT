from ixel_mat.schema.response import Confidence, parse_structured_response


VALID_JSON = '{"answer":"Use parameterized queries","confidence":"high","evidence":["OWASP"],"uncertainties":[],"commands":["pytest"],"next_step":"patch it"}'


def test_parse_structured_response_parses_valid_json_response():
    parsed = parse_structured_response("agent1", VALID_JSON, latency_ms=42)

    assert parsed.agent == "agent1"
    assert parsed.answer == "Use parameterized queries"
    assert parsed.confidence == Confidence.HIGH
    assert parsed.evidence == ["OWASP"]
    assert parsed.commands == ["pytest"]
    assert parsed.followup == "patch it"
    assert parsed.degraded is False
    assert parsed.latency_ms == 42


def test_parse_structured_response_marks_unstructured_text_as_degraded():
    parsed = parse_structured_response("agent2", "503 Service Unavailable", latency_ms=9)

    assert parsed.degraded is True
    assert parsed.answer == "503 Service Unavailable"
    assert parsed.degraded_reason


def test_parse_structured_response_handles_empty_response_as_degraded():
    parsed = parse_structured_response("agent3", "", latency_ms=0)

    assert parsed.degraded is True
    assert parsed.answer == ""
    assert parsed.confidence == Confidence.UNCERTAIN
