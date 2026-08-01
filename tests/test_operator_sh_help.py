"""The two places operator.sh describes its own injected flags must be true.

`operator.sh` states the flags it adds in three places -- the header comment,
the `--help` text, and the `local copilot_args=(...)` line that actually does
it -- and only the third one runs. The other two had been wrong since
`--effort high` and `--experimental` were added: both still advertised loop
mode as adding `--yolo --autopilot --no-ask-user`, and neither mentioned that
single-session mode injects anything at all, which it does.

That is not cosmetic. `--experimental` is what loads the extensions, and
`--yolo` is a blanket approval grant; a user reading `--help` to find out what
this script does to their session on their behalf was being told something
false about both. Documentation that describes an argument list is code that
nothing executes, so it rots silently and in exactly one direction.

These tests derive the expected sentence from the array that really runs, so
the docs cannot drift from it again: change the defaults and the docs fail
until they are updated to match.
"""
import re

import pytest

from test_operator import OPERATOR_SH, _shell_function


def _injected_flags(function: str) -> str:
    """The defaults `function` builds, as they would read in prose.

    Reads the real `local copilot_args=(...)` line rather than a copy of the
    list, so this cannot certify docs against a second stale transcription.
    """
    body = _shell_function(function)
    match = re.search(r"^\s*local copilot_args=\((.*?)\)\s*$", body, re.MULTILINE)
    assert match, f"{function}() no longer builds copilot_args in one line"
    elements = re.findall(r'"([^"]*)"', match.group(1))
    assert elements, f"{function}() builds copilot_args from nothing"
    assert any(e.startswith("--") for e in elements), (
        f"{function}() builds copilot_args with no flags at all: {elements}")
    return " ".join(elements)


@pytest.mark.parametrize("function", ["run_single_session", "run_loop_mode"])
def test_help_text_lists_the_flags_that_are_really_injected(function):
    """`--help` must name every flag, in the order the launch really uses.

    Order is asserted with the flags because the help is read as a single
    phrase; a set comparison would accept a sentence that lists them in an
    order the script does not use, which is precisely how someone reasons
    wrongly about which spelling wins a conflict.
    """
    text = OPERATOR_SH.read_text(encoding="utf-8")
    sentence = f"Adds {_injected_flags(function)} automatically."
    assert sentence in text, (
        f"operator.sh --help does not describe {function}()'s real defaults.\n"
        f"  expected: {sentence}")


def test_header_comment_describes_loop_mode_accurately():
    """The comment block at the top of the file, which is what a reader hits
    before they ever reach `--help`."""
    text = OPERATOR_SH.read_text(encoding="utf-8")
    loop = _injected_flags("run_loop_mode")
    assert f"# Loop mode (--loop) adds {loop}," in text, (
        f"the header comment does not describe loop mode's real defaults.\n"
        f"  expected the list: {loop}")

    single = _injected_flags("run_single_session")
    assert f"# It adds {single}." in text, (
        f"the header comment does not describe single-session's real "
        f"defaults.\n  expected the list: {single}")
