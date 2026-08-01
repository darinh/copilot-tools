"""A subprocess call that decodes must name both its encoding and its errors.

``text=True`` does not mean "give me a string". It means "decode with
``locale.getpreferredencoding(False)``", which on Windows is a legacy ANSI
codepage — cp1252 here — while every tool this repository shells out to emits
UTF-8. The two agree on ASCII and nowhere else.

**The failure mode is the reason this is a scan and not a style rule.** When
the decode fails it does not raise at the call. With ``capture_output=True``
the decode happens on a reader thread, so ``subprocess.run`` returns
*normally*, ``returncode`` is **0**, and ``stdout`` is **None**. Measured on
this repo, 2026-08-01, with a U+0401 in a repository path (UTF-8 ``d0 81``;
0x81 is undefined in cp1252)::

    Exception in thread Thread-9 (_readerthread):
    UnicodeDecodeError: 'charmap' codec can't decode byte 0x81 ...
    returncode: 0
    stdout is None: True

Every guard in this repository is written as ``if proc.returncode != 0``, and
that guard passes. The error arrives later and somewhere else, as an
``AttributeError`` on ``None`` — and in the worst instance,
``project_paths.primary_repo_root``, "git could not tell me the repo root"
was rendered as a crash, when the function's entire contract is to fall back
to its argument. The nearby failure mode is quieter and worse: a
``proc.stdout or ""`` turns the same event into *the command succeeded and
said nothing*, which is the collapse this repository's conventions single out
by name.

**Both halves are required, and the second half is not obvious.** Naming
``encoding="utf-8"`` alone leaves the codec *strict*, so undecodable bytes
still kill the reader thread and still hand back ``stdout is None``. A peer
agent hit exactly this on 2026-08-01 while checking the first half of the fix
(``UnicodeDecodeError: 'utf-8' codec can't decode byte 0x97``, an em-dash from
a Windows console) and reported it back: ``encoding=`` alone is not the fix.
So the rule demands ``errors=`` too. Any value is accepted — an explicit
``errors="strict"`` is a deliberate choice and reads as one. What is rejected
is *inheriting* a policy nobody chose.

**Scope.** A call that never decodes is not in scope: ``capture_output=True``
with no text kwarg returns bytes, cannot raise on decode, and is a perfectly
good way to run a command. The rule only binds once a call has asked Python
to turn bytes into ``str``.

**Tests are covered too, deliberately.** ``tests/`` is where temporary
directories with generated names get handed to git, and a test that dies in a
reader thread fails for a reason that has nothing to do with what it asserts.
The sibling scan for unreachable code makes the same call for the same
reason; the presence-probe scan excludes tests because "cannot tell" is not a
state a fixture can be in, and that argument does not transfer — a fixture
path can absolutely contain a byte cp1252 has no glyph for.

**Escape hatch.** ``# decode-ok: <reason>`` on any line of the call. It must
be a real comment — the annotation is read from Python's own token stream,
not matched against the raw line, so a string *containing* the marker cannot
silence anything — and it must carry a reason, because an annotation with no
reason is an exemption nobody has to defend.

**Anti-vacuity.** Every detector in :data:`DETECTORS` is pinned to a control
that declares *which* detector it exercises, and the pinning is by identity:
a control that fires the wrong detector fails, which a "something fired"
assertion cannot see. :func:`test_every_detector_has_a_control` fails when a
detector is added without one. The repository population is asserted
non-empty and asserted to contain named files, because a filter bug that
empties it passes every per-file test above it. The annotation rules are
exercised against synthetic sources rather than the tree, because there are
no annotations in the tree today and a loop over none of them is green no
matter what it asserts.
"""
import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

NOT_SOURCE = (".git", ".worktrees", "node_modules", "__pycache__",
              ".specify", "build", "dist", ".venv", "venv")

#: Callables in ``subprocess`` that spawn a process and can be asked to decode.
SUBPROCESS_FUNCS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "getoutput",
     "getstatusoutput"})

#: Keywords that put a call into text mode. Any one of them is enough --
#: ``encoding=`` and ``errors=`` imply text mode on their own, which is why
#: "no ``text=True``" is not the same question as "does not decode".
DECODING_KWARGS = frozenset({"text", "universal_newlines", "encoding",
                             "errors"})

#: What each detector means, in the words a failure message should use.
DETECTORS = {
    "locale-decoded": (
        "decodes with locale.getpreferredencoding() (cp1252 on Windows) "
        "because no encoding= was named; pass encoding='utf-8'"
    ),
    "strict-codec": (
        "names an encoding but no errors= policy, so the codec is strict and "
        "an undecodable byte still kills the reader thread; pass "
        "errors='replace'"
    ),
}

_ANNOTATION = re.compile(r"#\s*decode-ok\s*:\s*(?P<reason>\S.*?)\s*$")


def _python_sources() -> list[Path]:
    """Every ``*.py`` in the repository, discovered rather than listed."""
    out = []
    for path in REPO.rglob("*.py"):
        # Filter on the repo-relative path. An absolute-path filter matches
        # ".worktrees" for every file when the checkout *is* a worktree, and
        # an empty population passes every assertion below.
        rel = path.relative_to(REPO)
        if any(part in NOT_SOURCE for part in rel.parts):
            continue
        out.append(path)
    return sorted(out)


def _annotated_lines(source: str) -> tuple[set[int], set[int]]:
    """``(every annotated line, those whose line holds nothing else)``.

    Read from the token stream on purpose. A regex over raw lines cannot tell
    a comment from a string that contains the same characters, so any file
    could silence the scan by mentioning the marker in a docstring -- this
    one included.

    The two sets are kept apart because they license different things. A
    comment sharing a line with code annotates that code. A comment sitting on
    its own line annotates what follows it, and only then -- otherwise the
    last line of one call could exempt the call that starts on the next.
    """
    every: set[int] = set()
    own: set[int] = set()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            if not _ANNOTATION.search(token.string):
                continue
            line = token.start[0]
            every.add(line)
            if not token.line[:token.start[1]].strip():
                own.add(line)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return set(), set()
    return every, own


def _subprocess_names(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """``(module aliases, {local name: subprocess function})``.

    ``import subprocess as sp`` and ``from subprocess import run`` are the
    same call wearing different clothes. A scan that only knows the
    ``subprocess.run`` spelling reports the loud form and permits the quiet
    one, which is worse than not scanning: it certifies the quiet one.
    """
    modules: set[str] = set()
    funcs: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess" and not node.level:
                for alias in node.names:
                    if alias.name in SUBPROCESS_FUNCS:
                        funcs[alias.asname or alias.name] = alias.name
    return modules, funcs


def _spawn_calls(tree: ast.AST) -> list[tuple[ast.Call, str]]:
    """Every call in ``tree`` that spawns a process, with its function name."""
    modules, funcs = _subprocess_names(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id in modules \
                    and func.attr in SUBPROCESS_FUNCS:
                found.append((node, func.attr))
        elif isinstance(func, ast.Name) and func.id in funcs:
            found.append((node, funcs[func.id]))
    return found


def _keyword(call: ast.Call, name: str) -> ast.keyword | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def _is_off(kw: ast.keyword | None) -> bool:
    """True when the keyword is absent or explicitly ``False``/``None``.

    A keyword whose value is a *variable* is not off: it may be truthy at run
    time, and a scan that assumed otherwise would let ``text=flag`` through.
    """
    if kw is None:
        return True
    value = kw.value
    return isinstance(value, ast.Constant) and value.value in (False, None)


def undecoded_calls(source: str) -> list[tuple[int, str, str]]:
    """``(line, detector, function)`` for each spawn that decodes unsafely.

    A call is in scope once any of :data:`DECODING_KWARGS` is active. In
    scope, it must name ``encoding=`` (else ``locale-decoded``) and must name
    ``errors=`` (else ``strict-codec``). At most one finding per call: the
    missing encoding is the finding a reader needs first, and reporting both
    for one call would double-count it in every coverage sum.
    """
    tree = ast.parse(source)
    annotated, own_line = _annotated_lines(source)
    found = []
    for call, name in _spawn_calls(tree):
        if all(_is_off(_keyword(call, kw)) for kw in DECODING_KWARGS):
            continue  # bytes in, bytes out: nothing to decode, nothing to get wrong
        span = set(range(call.lineno, (call.end_lineno or call.lineno) + 1))
        if (annotated & span) or (call.lineno - 1) in own_line:
            continue
        if _is_off(_keyword(call, "encoding")):
            found.append((call.lineno, "locale-decoded", name))
        elif _is_off(_keyword(call, "errors")):
            found.append((call.lineno, "strict-codec", name))
    return sorted(found)


def _scan(path: Path) -> list[tuple[int, str, str]]:
    return undecoded_calls(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", _python_sources(),
                         ids=lambda p: str(p.relative_to(REPO)).replace("\\", "/"))
def test_every_decoding_subprocess_call_names_its_codec(path):
    bad = _scan(path)
    assert not bad, "\n".join(
        f"{path.relative_to(REPO)}:{line}: subprocess.{func} {DETECTORS[det]}"
        for line, det, func in bad
    )


def test_the_population_is_not_empty_and_holds_what_we_ship():
    """A filter bug that empties the population passes every test above it."""
    found = {str(p.relative_to(REPO)).replace("\\", "/")
             for p in _python_sources()}
    assert len(found) > 20, f"suspiciously few Python files: {sorted(found)}"
    for expected in ("project_paths.py", "setup_tools.py", "git_identity.py",
                     "operator_runner.py", "e2e_restart_loop.py",
                     "tests/test_operator.py"):
        assert expected in found, f"{expected} missing from the scan"


def test_the_scan_actually_examines_subprocess_calls():
    """Non-empty *files* is not the same claim as non-empty *calls*.

    The per-file test is green for a file with no ``subprocess`` in it, so a
    defect in :func:`_spawn_calls` -- a name-resolution bug, a typo in
    :data:`SUBPROCESS_FUNCS` -- would leave every assertion above green while
    the scan examined nothing at all.
    """
    per_file = {}
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = _spawn_calls(tree)
        if calls:
            per_file[str(path.relative_to(REPO)).replace("\\", "/")] = len(calls)
    assert sum(per_file.values()) > 30, per_file
    for expected in ("project_paths.py", "setup_tools.py", "git_identity.py"):
        assert per_file.get(expected), f"no spawn calls found in {expected}"


#: Each control declares the ONE detector it exists to exercise. Asserting
#: that "a" detector fired cannot tell a control aimed at the right rule from
#: one aimed at another that happens to also match; swapping two such control
#: bodies leaves every test green. The declaration is what
#: :func:`test_every_detector_has_a_control` reads, so a new detector without
#: a control fails rather than silently going untested.
FIRES = {
    "text=True with no encoding": ("locale-decoded", """
import subprocess
subprocess.run(["git", "status"], capture_output=True, text=True)
"""),
    "universal_newlines=True with no encoding": ("locale-decoded", """
import subprocess
subprocess.run(["ls"], capture_output=True, universal_newlines=True)
"""),
    "errors= alone still decodes with the locale": ("locale-decoded", """
import subprocess
subprocess.run(["ls"], capture_output=True, errors="replace")
"""),
    "text mode via a Popen": ("locale-decoded", """
import subprocess
subprocess.Popen(["ls"], stdout=subprocess.PIPE, text=True)
"""),
    "check_output in text mode": ("locale-decoded", """
import subprocess
subprocess.check_output(["ls"], text=True)
"""),
    "aliased module import": ("locale-decoded", """
import subprocess as sp
sp.run(["ls"], capture_output=True, text=True)
"""),
    "from-import of run": ("locale-decoded", """
from subprocess import run
run(["ls"], capture_output=True, text=True)
"""),
    "from-import under an alias": ("locale-decoded", """
from subprocess import run as launch
launch(["ls"], capture_output=True, text=True)
"""),
    "text= bound to a variable is not proof it is off": ("locale-decoded", """
import subprocess
def go(flag):
    return subprocess.run(["ls"], capture_output=True, text=flag)
"""),
    "encoding named but errors left strict": ("strict-codec", """
import subprocess
subprocess.run(["git", "log"], capture_output=True, encoding="utf-8")
"""),
    "text=True and encoding but no errors": ("strict-codec", """
import subprocess
subprocess.run(["ls"], capture_output=True, text=True, encoding="utf-8")
"""),
    "errors explicitly disabled with None": ("strict-codec", """
import subprocess
subprocess.run(["ls"], capture_output=True, encoding="utf-8", errors=None)
"""),
}

#: Spellings that are correct and must not be reported.
PASSES = {
    "the house pattern": """
import subprocess
subprocess.run(["git", "log"], capture_output=True,
               encoding="utf-8", errors="replace")
""",
    "explicit strict is a choice, not an inheritance": """
import subprocess
subprocess.run(["ls"], capture_output=True, encoding="utf-8",
               errors="strict")
""",
    "bytes in, bytes out": """
import subprocess
subprocess.run(["ls"], capture_output=True)
""",
    "no capture at all": """
import subprocess
subprocess.run(["ls"])
""",
    "text explicitly False": """
import subprocess
subprocess.run(["ls"], capture_output=True, text=False)
""",
    "a call to something else entirely": """
import subprocess
run(["ls"], capture_output=True, text=True)
""",
    "a method named run on an unrelated object": """
import subprocess
runner.run(["ls"], capture_output=True, text=True)
""",
    "annotated with a reason": """
import subprocess
# decode-ok: the callee is a fixture that only ever emits ASCII
subprocess.run(["ls"], capture_output=True, text=True)
""",
    "annotation on the closing line of a multi-line call": """
import subprocess
subprocess.run(
    ["ls"],
    capture_output=True,
    text=True,  # decode-ok: ASCII-only by construction
)
""",
}


@pytest.mark.parametrize("name", sorted(FIRES))
def test_the_detector_fires_and_it_is_the_declared_one(name):
    detector, source = FIRES[name]
    assert detector in DETECTORS, f"{name!r} declares an unknown detector"
    found = undecoded_calls(source)
    assert found, (
        f"the detector did not report {name!r}; a detector that matches "
        "nothing reports the whole tree clean"
    )
    fired = {det for _, det, _ in found}
    assert fired == {detector}, (
        f"{name!r} exists to exercise {detector!r} but fired {sorted(fired)}; "
        "a control aimed at the wrong detector proves nothing about the one "
        "it is named after"
    )


@pytest.mark.parametrize("name", sorted(PASSES))
def test_the_detector_leaves_correct_code_alone(name):
    assert not undecoded_calls(PASSES[name]), (
        f"{name!r} is ordinary correct code and was reported"
    )


def test_every_detector_has_a_control():
    """Adding a detector without a control must fail, not pass quietly."""
    declared = {detector for detector, _ in FIRES.values()}
    assert declared == set(DETECTORS), (
        f"detectors without a control: {sorted(set(DETECTORS) - declared)}; "
        f"controls for unknown detectors: {sorted(declared - set(DETECTORS))}"
    )


def test_one_finding_per_call_even_when_both_rules_are_broken():
    found = undecoded_calls("""
import subprocess
subprocess.run(["ls"], capture_output=True, text=True)
""")
    assert len(found) == 1, found
    assert found[0][1] == "locale-decoded"


def test_the_annotation_must_carry_a_reason():
    """Exercised on synthetic source: the tree holds no annotations today, so
    a loop over the real ones is green whatever it asserts."""
    bare = """
import subprocess
# decode-ok
subprocess.run(["ls"], capture_output=True, text=True)
"""
    colon_but_empty = """
import subprocess
# decode-ok:
subprocess.run(["ls"], capture_output=True, text=True)
"""
    assert undecoded_calls(bare), "a reasonless annotation silenced the scan"
    assert undecoded_calls(colon_but_empty), \
        "an empty reason silenced the scan"


def test_a_string_containing_the_marker_does_not_silence_the_scan():
    """The annotation is a comment token, not a substring of the line.

    The string sits *on the call's own line* deliberately. A marker in a
    docstring two lines up is refused by the line arithmetic whether or not
    the scan reads tokens, so it cannot tell a token-aware implementation from
    a regex over raw lines -- measured: a mutation that accepted STRING tokens
    as annotations survived that version of this test.
    """
    on_the_call_line = (
        'import subprocess\n'
        'subprocess.run(["ls"], capture_output=True, text=True,\n'
        '               env={"NOTE": "# decode-ok: not a comment"})\n'
    )
    assert undecoded_calls(on_the_call_line), \
        "a string literal silenced the scan; read comments, not raw lines"

    above_the_call = (
        'import subprocess\n'
        'MARKER = "# decode-ok: not a comment, just text"\n'
        'subprocess.run(["ls"], capture_output=True, text=True)\n'
    )
    assert undecoded_calls(above_the_call), \
        "a string literal on the preceding line silenced the scan"


def test_an_annotation_silences_only_the_call_it_sits_on():
    source = """
import subprocess
# decode-ok: this one is fine
subprocess.run(["ls"], capture_output=True, text=True)
subprocess.run(["pwd"], capture_output=True, text=True)
"""
    found = undecoded_calls(source)
    assert len(found) == 1, found
    assert found[0][0] == 5, found
