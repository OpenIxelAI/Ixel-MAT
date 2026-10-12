"""An edit to a file with Windows line endings, sent with plain newlines the way models write them."""
from ixel_mat.agent.tools import Workspace


def test_a_multi_line_edit_matches_a_crlf_file_and_keeps_its_endings(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    ws = Workspace(tmp_path)
    ws.apply(ws.prepare_edit("a.txt", "one\ntwo\n", "uno\ndos\n"))
    assert (tmp_path / "a.txt").read_bytes() == b"uno\r\ndos\r\nthree\r\n"


def test_a_plain_newline_file_is_left_as_it_is(tmp_path):
    (tmp_path / "b.txt").write_bytes(b"one\ntwo\n")
    ws = Workspace(tmp_path)
    ws.apply(ws.prepare_edit("b.txt", "one\ntwo", "uno\ndos"))
    assert (tmp_path / "b.txt").read_bytes() == b"uno\ndos\n"
