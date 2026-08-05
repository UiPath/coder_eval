"""Tests for coder_eval.integrity: graded-material derivation and the read scan.

The bulk of the integrity work is classification, so most of this is table-driven
over shell strings. The cases that matter most are the NEGATIVE ones: a scan that
flags a directory listing is worse than no scan, because it voids honest rows and
gets switched off.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from coder_eval.integrity import (
    GradedMaterialSpec,
    _bash_read,
    _task_dir_operands,
    derive_graded_material,
    evaluate_integrity,
    scan_commands,
)
from coder_eval.models import (
    CommandTelemetry,
    IntegrityFindingKind,
    IntegrityMode,
    IntegrityVerdict,
    TaskDefinition,
    TurnRecord,
)
from coder_eval.models.container_paths import CONTAINER_INPUT_DIR


SPEC = GradedMaterialSpec(
    paths=frozenset({"/repo/tasks/leaky/task.yaml", "/repo/tasks/leaky/solution.py", "$TASK_DIR/check_output.py"}),
    directories=frozenset({"/repo/tasks/leaky/_reference", CONTAINER_INPUT_DIR}),
    basename_globs=("RESOLUTION.md", "*.expected", "task.yaml", "context.json"),
    grader_globs=("check_*.py", "check.py"),
    task_dir="/repo/tasks/leaky",
)


def _cmd(tool_name: str, parameters: dict, tool_id: str = "t0") -> CommandTelemetry:
    return CommandTelemetry(
        tool_name=tool_name, tool_id=tool_id, timestamp=datetime.now(), parameters=parameters, result_status="success"
    )


def _bash(command: str) -> CommandTelemetry:
    return _cmd("Bash", {"command": command})


def _turn(commands: list[CommandTelemetry], *, iteration: int = 1, unrecovered: int = 0) -> TurnRecord:
    return TurnRecord(
        iteration=iteration,
        user_input="p",
        agent_output="a",
        commands=commands,
        unrecovered_subagent_threads=unrecovered,
    )


# --------------------------------------------------------------------------
# Shell segment classification
# --------------------------------------------------------------------------

READS = [
    pytest.param("cat RESOLUTION.md", id="cat"),
    pytest.param("head -50 RESOLUTION.md", id="head"),
    pytest.param("tail -n 5 /repo/tasks/leaky/solution.py", id="tail-absolute-path"),
    pytest.param("sed -n '1,20p' RESOLUTION.md", id="sed"),
    pytest.param("awk '{print}' /repo/tasks/leaky/check_output.py", id="awk-grader-under-the-task-dir"),
    pytest.param("cat /repo/tests/tasks/other/check.py", id="grader-without-an-underscore"),
    pytest.param("cat $TASK_DIR/check.py", id="grader-under-the-task-dir-variable"),
    pytest.param("python3 $TASK_DIR/check_output.py", id="python-runs-the-grader"),
    pytest.param("python -c \"print(open('RESOLUTION.md').read())\"", id="python-inline"),
    pytest.param("node -e \"require('fs').readFileSync('RESOLUTION.md')\"", id="node-inline"),
    pytest.param("base64 -d RESOLUTION.md", id="base64"),
    pytest.param("xxd RESOLUTION.md | head", id="xxd-piped"),
    pytest.param("jq . /work/input/context.json", id="jq-container-input"),
    pytest.param("diff mine.py /repo/tasks/leaky/solution.py", id="diff-against-reference"),
    pytest.param("cp /repo/tasks/leaky/solution.py ./mine.py", id="cp-the-reference"),
    pytest.param("grep -n 'root cause' RESOLUTION.md", id="grep-content"),
    pytest.param("grep cat RESOLUTION.md", id="grep-content-with-a-reader-named-pattern"),
    pytest.param("rg 'fixed version' RESOLUTION.md", id="rg-content"),
    pytest.param("grep -- '-c' RESOLUTION.md", id="count-flag-behind-end-of-options"),
    pytest.param("grep -e -l RESOLUTION.md", id="files-flag-as-a-pattern-operand"),
    pytest.param("rg --regexp -c RESOLUTION.md", id="count-flag-as-a-long-option-operand"),
    pytest.param("while read l; do echo $l; done < RESOLUTION.md", id="input-redirect"),
    pytest.param("find . -name '*.md' -exec cat RESOLUTION.md {} \\;", id="find-exec-cat"),
    pytest.param("ls -1 | xargs cat RESOLUTION.md", id="xargs-cat"),
    pytest.param("sudo cat RESOLUTION.md", id="sudo-wrapper"),
    pytest.param("FOO=1 cat RESOLUTION.md", id="env-assignment-prefix"),
    pytest.param("ls -la && cat RESOLUTION.md", id="second-segment-reads"),
    pytest.param("cat /repo/tasks/leaky/_reference/answer.py", id="reference-directory"),
    pytest.param("strange-tool RESOLUTION.md", id="unknown-utility-conservative"),
]

NOT_READS = [
    pytest.param("ls -la", id="plain-listing"),
    pytest.param("ls -la /repo/tasks/leaky", id="listing-the-task-dir"),
    pytest.param("find . -name RESOLUTION.md", id="find-by-name"),
    pytest.param("find /repo -name 'check_*.py' -print", id="find-glob-print"),
    pytest.param("test -f RESOLUTION.md", id="existence-test"),
    pytest.param("stat RESOLUTION.md", id="stat-metadata"),
    pytest.param("wc -l RESOLUTION.md", id="wc-line-count"),
    pytest.param("basename /repo/tasks/leaky/task.yaml", id="basename"),
    pytest.param("dirname /repo/tasks/leaky/task.yaml", id="dirname"),
    pytest.param("echo RESOLUTION.md", id="echo-the-name"),
    pytest.param("grep -l 'cause' RESOLUTION.md", id="grep-files-only"),
    pytest.param("grep -l cat RESOLUTION.md", id="grep-files-only-with-a-reader-named-pattern"),
    pytest.param("rg -c 'head|tail' RESOLUTION.md", id="rg-count-with-reader-named-alternation"),
    pytest.param("grep -rl 'cause' /repo --include=RESOLUTION.md", id="grep-bundled-files-only"),
    pytest.param("grep -c 'cause' RESOLUTION.md", id="grep-count"),
    pytest.param("grep -m 5 -c 'cause' RESOLUTION.md", id="grep-count-after-a-valued-option"),
    pytest.param("rg -tpy -c cause RESOLUTION.md", id="rg-count-after-an-inline-valued-bundle"),
    pytest.param("rg --files /repo | grep -l RESOLUTION.md", id="rg-files"),
    pytest.param("rg --files-with-matches cause RESOLUTION.md", id="rg-files-with-matches"),
    pytest.param("cat my_own_notes.md", id="reads-something-else"),
    pytest.param("python3 build.py", id="runs-own-script"),
    pytest.param("mkdir -p output && ls", id="unrelated-work"),
    pytest.param("du -sh /repo/tasks/leaky", id="disk-usage"),
]


@pytest.mark.parametrize("command", READS)
def test_shell_reads_are_flagged(command: str):
    is_read, matched = _bash_read(command, SPEC)
    assert is_read is True, f"expected a read: {command!r}"
    assert matched is not None


@pytest.mark.parametrize("command", NOT_READS)
def test_shell_non_reads_are_not_flagged(command: str):
    is_read, _ = _bash_read(command, SPEC)
    assert is_read is False, f"expected NOT a read: {command!r}"


# --------------------------------------------------------------------------
# False positives: an honest command that only LOOKS like a leak
# --------------------------------------------------------------------------

FALSE_POSITIVES = [
    pytest.param("cat > check_env.py", id="writing-your-own-helper"),
    pytest.param("cat >> /repo/tasks/leaky/check_env.py", id="appending-to-a-task-dir-path"),
    pytest.param("cat > RESOLUTION.md", id="writing-the-deliverable"),
    pytest.param("rm -f check_env.py", id="removing-your-own-helper"),
    pytest.param("rm -f /repo/tasks/leaky/check_env.py", id="removing-a-task-dir-path"),
    pytest.param("mv check_temp.py somewhere/", id="moving-your-own-helper"),
    pytest.param("mv /repo/tasks/leaky/check_temp.py somewhere/", id="moving-a-task-dir-path"),
    pytest.param("chmod +x /repo/tasks/leaky/check_env.py", id="chmod"),
    pytest.param("for f in check_*.py; do echo $f; done", id="globbing-your-own-helpers"),
    pytest.param(
        "uip solution resources get my-asset --decode --output json",
        id="legitimate-decode-flag",
    ),
]


@pytest.mark.parametrize("command", FALSE_POSITIVES)
def test_honest_commands_are_not_reads(command: str):
    """Under `void` these would each destroy a row that measured the agent fairly."""
    is_read, _ = _bash_read(command, SPEC)
    assert is_read is False, f"false positive: {command!r}"


# --------------------------------------------------------------------------
# git: neutral or a read depending on the subcommand
# --------------------------------------------------------------------------

_GRADER = "tests/tasks/suite/scen/check_thing.py"

GIT_READS = [
    pytest.param(f"git show HEAD:{_GRADER}", id="show"),
    pytest.param(f"git cat-file -p HEAD:{_GRADER}", id="cat-file"),
    pytest.param(f"git diff HEAD -- {_GRADER}", id="diff"),
    pytest.param(f"git blame {_GRADER}", id="blame"),
    pytest.param(f"git grep answer -- {_GRADER}", id="grep"),
    pytest.param(f"git log -p -- {_GRADER}", id="log-patch-short"),
    pytest.param(f"git log --patch -- {_GRADER}", id="log-patch-long"),
    pytest.param(f"git -C /repo show HEAD:{_GRADER}", id="global-option-with-a-value"),
    pytest.param(f"git archive HEAD {_GRADER}", id="unlisted-subcommand-defaults-to-read"),
    pytest.param(f"git {_GRADER}", id="no-identifiable-subcommand"),
]

GIT_NEUTRAL = [
    pytest.param("git add check_env.py", id="add-own-helper"),
    pytest.param(f"git add {_GRADER}", id="add"),
    pytest.param(f"git rm {_GRADER}", id="rm"),
    pytest.param(f"git mv {_GRADER} somewhere/", id="mv"),
    pytest.param(f"git checkout -- {_GRADER}", id="checkout"),
    pytest.param(f"git restore {_GRADER}", id="restore"),
    pytest.param(f"git log --oneline -- {_GRADER}", id="log-without-a-patch-flag"),
    pytest.param(f"git status --short {_GRADER}", id="status"),
    pytest.param(f'git commit -m "cat {_GRADER}"', id="commit-message-mentioning-it"),
    pytest.param("git stash", id="stash"),
    pytest.param("git branch -a", id="branch"),
    pytest.param("git config user.name someone", id="config"),
]


@pytest.mark.parametrize("command", GIT_READS)
def test_content_emitting_git_subcommands_are_reads(command: str):
    """`git show HEAD:<answer key>` prints the file as surely as `cat` does; a
    blanket-neutral `git` was a one-command bypass."""
    is_read, matched = _bash_read(command, SPEC)
    assert is_read is True, f"expected a read: {command!r}"
    assert matched is not None


@pytest.mark.parametrize("command", GIT_NEUTRAL)
def test_non_content_git_subcommands_are_not_reads(command: str):
    is_read, _ = _bash_read(command, SPEC)
    assert is_read is False, f"false positive: {command!r}"


def test_a_read_before_an_output_redirect_still_counts():
    """The write rule must not become an escape hatch: `cat KEY > mine` is a read."""
    is_read, _ = _bash_read("cat RESOLUTION.md > my_notes.md", SPEC)
    assert is_read is True


def test_stderr_redirection_does_not_hide_a_read():
    is_read, _ = _bash_read("cat RESOLUTION.md 2>&1", SPEC)
    assert is_read is True


# A redirect consumes exactly ONE word, and only when it is a real, unquoted
# operator -- otherwise `>` anywhere in the segment wrote off the whole tail.

REDIRECT_STILL_READS = [
    pytest.param("cat 2>/dev/null RESOLUTION.md", id="operand-after-a-stderr-redirect"),
    pytest.param("cat > /tmp/copy RESOLUTION.md", id="operand-after-the-write-target"),
    pytest.param("awk '$1 > 0 {print}' RESOLUTION.md", id="quoted-gt-in-an-awk-program"),
    pytest.param("sed -n '/x>y/p' RESOLUTION.md", id="quoted-gt-in-a-sed-pattern"),
    pytest.param("awk 'NR>0' RESOLUTION.md", id="quoted-gt-without-spaces"),
    pytest.param("cat RESOLUTION.md >> log.txt", id="append-redirect-after-the-operand"),
    pytest.param("cat <RESOLUTION.md", id="input-redirect-without-a-space"),
    pytest.param("head 'RESOLUTION.md' > out.txt", id="quoted-operand-before-the-redirect"),
]

REDIRECT_WRITES_ONLY = [
    pytest.param("echo diagnosis > RESOLUTION.md", id="write-target-only"),
    pytest.param("printf 'x' >>RESOLUTION.md", id="append-target-without-a-space"),
    pytest.param("ls 2> 'RESOLUTION.md'", id="quoted-write-target"),
]


@pytest.mark.parametrize("command", REDIRECT_STILL_READS)
def test_a_redirect_consumes_only_its_target_word(command: str):
    is_read, _ = _bash_read(command, SPEC)
    assert is_read is True, f"expected a read: {command!r}"


@pytest.mark.parametrize("command", REDIRECT_WRITES_ONLY)
def test_a_reference_only_in_a_write_target_is_not_a_read(command: str):
    is_read, _ = _bash_read(command, SPEC)
    assert is_read is False, f"false positive: {command!r}"


def test_windows_separators_still_match():
    """A task file recorded with backslashes must match a forward-slash command."""
    spec = GradedMaterialSpec(paths=frozenset({r"C:\repo\tasks\leaky\task.yaml"}))
    is_read, matched = _bash_read("cat C:/repo/tasks/leaky/task.yaml", spec)
    assert is_read is True
    assert matched == r"C:\repo\tasks\leaky\task.yaml"


def test_glob_wildcard_does_not_cross_a_path_separator():
    """`check_*.py` must not match `check_dir/unrelated.py`."""
    spec = GradedMaterialSpec(basename_globs=("check_*.py",))
    assert _bash_read("cat check_dir/unrelated.py", spec) == (False, None)
    assert _bash_read("cat check_dir/check_it.py", spec)[0] is True


# --------------------------------------------------------------------------
# A basename glob matches a WHOLE filename, not a substring of one
# --------------------------------------------------------------------------

GLOB_BOUNDARY_READS = [
    pytest.param("cat RESOLUTION.md", id="bare"),
    pytest.param("sed -n '1,20p' ../scen/RESOLUTION.md", id="relative-parent-path"),
    pytest.param("cat ./RESOLUTION.md", id="dot-slash"),
    pytest.param("cat 'RESOLUTION.md'", id="single-quoted"),
    pytest.param('cat "RESOLUTION.md"', id="double-quoted"),
    pytest.param("head RESOLUTION.md;echo done", id="trailing-semicolon"),
    pytest.param("head RESOLUTION.md|head -3", id="trailing-pipe"),
    pytest.param("cat /repo/tasks/leaky/check_env.py", id="grader-under-the-task-dir"),
    pytest.param("python3 $TASK_DIR/check.py", id="grader-under-the-task-dir-variable"),
    pytest.param("cat out.expected", id="suffix-glob"),
    pytest.param(r"cat C:\repo\tasks\leaky\RESOLUTION.md", id="windows-separators"),
]

GLOB_BOUNDARY_CLEAN = [
    pytest.param("cat RESOLUTION.md.draft", id="own-draft-of-the-deliverable"),
    pytest.param("cat notes/RESOLUTION.md-backup.txt", id="own-backup-name"),
    pytest.param("python tests/tasks/suite/scen/check_env.pyc", id="compiled-bytecode-sibling"),
    pytest.param("cat my-task.yaml.bak", id="longer-name-ending-in-the-glob"),
    pytest.param("cat my_task.yaml", id="longer-name-prefixed-with-a-word-char"),
    pytest.param("cat out.expected.tmp", id="suffix-glob-with-a-longer-name"),
]


@pytest.mark.parametrize("command", GLOB_BOUNDARY_READS)
def test_a_whole_filename_match_is_still_a_read(command: str):
    is_read, matched = _bash_read(command, SPEC)
    assert is_read is True, f"expected a read: {command!r}"
    assert matched is not None


@pytest.mark.parametrize("command", GLOB_BOUNDARY_CLEAN)
def test_a_glob_inside_a_longer_filename_is_not_a_match(command: str):
    """`RESOLUTION.md` inside `RESOLUTION.md.draft` is the agent's own scratch file;
    under `void` flagging it destroys an honest row."""
    assert _bash_read(command, SPEC) == (False, None)


def test_unbalanced_quotes_do_not_skip_the_command():
    """A command shlex cannot parse still ran, so it must still be classified."""
    is_read, _ = _bash_read("cat 'RESOLUTION.md", SPEC)
    assert is_read is True


# --------------------------------------------------------------------------
# Literal paths and directories match whole paths, not substrings of them
# --------------------------------------------------------------------------

LITERAL_BOUNDARY_CLEAN = [
    pytest.param("cat /repo/tasks/leaky/solution.py.bak", id="own-backup-of-the-reference-name"),
    pytest.param("cat /repo/tasks/leaky/solution.pyc", id="longer-extension"),
    pytest.param("cat /repo/tasks/leaky/_reference_notes/output.txt", id="sibling-dir-sharing-the-prefix"),
    pytest.param("cat /repo/tasks/leaky/_reference2/x.txt", id="sibling-dir-with-a-suffix-char"),
]

LITERAL_BOUNDARY_READS = [
    pytest.param("cat /repo/tasks/leaky/solution.py", id="the-reference-file-itself"),
    pytest.param("head '/repo/tasks/leaky/solution.py'", id="quoted"),
    pytest.param("cat /repo/tasks/leaky/_reference/answer.py", id="inside-the-reference-directory"),
    pytest.param("strange-tool /repo/tasks/leaky/_reference", id="the-reference-directory-itself"),
]


@pytest.mark.parametrize("command", LITERAL_BOUNDARY_CLEAN)
def test_a_literal_path_does_not_match_inside_a_longer_name(command: str):
    """`solution.py.bak` and `_reference_notes/` are the agent's own files; under
    `void` a substring match on them destroys an honest row."""
    assert _bash_read(command, SPEC) == (False, None)


@pytest.mark.parametrize("command", LITERAL_BOUNDARY_READS)
def test_a_whole_literal_path_still_matches(command: str):
    is_read, matched = _bash_read(command, SPEC)
    assert is_read is True, f"expected a read: {command!r}"
    assert matched is not None


# --------------------------------------------------------------------------
# Segmentation: quoting, comments and heredocs make text inert, not a command
# --------------------------------------------------------------------------

INERT_TEXT_CLEAN = [
    pytest.param("printf '%s\\n' 'harmless; cat RESOLUTION.md'", id="quoted-separator-in-an-argument"),
    pytest.param('echo "see; cat RESOLUTION.md" > notes.txt', id="double-quoted-separator"),
    pytest.param("echo done # cat RESOLUTION.md", id="trailing-comment"),
    pytest.param("# cat RESOLUTION.md", id="whole-line-comment"),
    pytest.param("cat > notes.txt <<'EOF'\ncat RESOLUTION.md\nEOF", id="quoted-heredoc-body-is-data"),
    pytest.param('cat > notes.txt <<"EOF"\ncat RESOLUTION.md\nEOF', id="double-quoted-heredoc-delimiter"),
    pytest.param("cat > notes.txt <<\\EOF\ncat RESOLUTION.md\nEOF", id="backslash-heredoc-delimiter"),
    pytest.param("cat > notes.txt <<-'EOF'\n\tcat RESOLUTION.md\n\tEOF", id="dash-heredoc-strips-tabs"),
]

INERT_TEXT_STILL_READS = [
    pytest.param("echo 'x; y' && cat RESOLUTION.md", id="quoting-does-not-hide-the-next-segment"),
    pytest.param("echo done#not-a-comment; cat RESOLUTION.md", id="hash-glued-to-a-word-is-not-a-comment"),
    pytest.param("cat > x <<'EOF'\ndata\nEOF\ncat RESOLUTION.md", id="command-after-the-heredoc-terminator"),
    pytest.param("cat > x <<EOF\n$(cat RESOLUTION.md)\nEOF", id="unquoted-heredoc-body-expands"),
]


@pytest.mark.parametrize("command", INERT_TEXT_CLEAN)
def test_inert_shell_text_is_not_a_command(command: str):
    """Data lines inside quotes, comments and quoted-delimiter heredocs never ran;
    under `void` each of these destroys an honest row."""
    is_read, _ = _bash_read(command, SPEC)
    assert is_read is False, f"false positive: {command!r}"


@pytest.mark.parametrize("command", INERT_TEXT_STILL_READS)
def test_inert_text_handling_does_not_hide_a_real_read(command: str):
    is_read, _ = _bash_read(command, SPEC)
    assert is_read is True, f"expected a read: {command!r}"


# --------------------------------------------------------------------------
# The agent's own deliverable: created-in-transcript paths are not answer keys
# --------------------------------------------------------------------------


def test_the_write_read_edit_deliverable_flow_is_clean():
    """The flagship troubleshoot flow: the agent writes RESOLUTION.md, and the
    harness's Read-before-Edit rule forces it to read its own copy back. Flagging
    that floods detect-mode triage and blocks void mode outright."""
    commands = [
        _cmd("Write", {"file_path": "/workspace/RESOLUTION.md", "content": "my diagnosis"}, tool_id="t1"),
        _cmd("Read", {"file_path": "/workspace/RESOLUTION.md"}, tool_id="t2"),
        _cmd("Edit", {"file_path": "/workspace/RESOLUTION.md", "old_string": "my", "new_string": "the"}, tool_id="t3"),
    ]
    info = scan_commands([_turn(commands)], SPEC)
    assert info.verdict is IntegrityVerdict.CLEAN
    assert info.findings == []


def test_a_shell_created_deliverable_can_be_re_read():
    info = scan_commands([_turn([_bash("echo 'diagnosis' > RESOLUTION.md"), _bash("cat RESOLUTION.md")])], SPEC)
    assert info.verdict is IntegrityVerdict.CLEAN


def test_creation_and_re_read_in_one_command_is_clean():
    info = scan_commands([_turn([_bash("cat > RESOLUTION.md && cat RESOLUTION.md")])], SPEC)
    assert info.verdict is IntegrityVerdict.CLEAN


def test_a_relative_re_read_of_an_absolutely_created_deliverable_is_clean():
    commands = [
        _cmd("Write", {"file_path": "/workspace/RESOLUTION.md", "content": "d"}, tool_id="t1"),
        _bash("cat RESOLUTION.md"),
    ]
    info = scan_commands([_turn(commands)], SPEC)
    assert info.verdict is IntegrityVerdict.CLEAN


def test_creating_your_own_copy_does_not_license_reading_a_golden():
    """Provenance is per-path: the agent's RESOLUTION.md excuses nothing at any
    OTHER location, or writing your own copy once would unlock every golden."""
    info = scan_commands(
        [_turn([_bash("echo 'diagnosis' > RESOLUTION.md"), _bash("cat ../scenario/RESOLUTION.md")])], SPEC
    )
    assert info.verdict is IntegrityVerdict.TAINTED


def test_a_relative_creation_never_excuses_an_absolute_read():
    info = scan_commands(
        [_turn([_bash("echo 'diagnosis' > RESOLUTION.md"), _bash("cat /repo/tests/tasks/scen/RESOLUTION.md")])], SPEC
    )
    assert info.verdict is IntegrityVerdict.TAINTED


def test_an_append_is_not_a_creation():
    """`>>` leaves the original content readable, so it proves nothing about
    who wrote the file."""
    info = scan_commands([_turn([_bash("echo 'note' >> RESOLUTION.md"), _bash("cat RESOLUTION.md")])], SPEC)
    assert info.verdict is IntegrityVerdict.TAINTED


def test_the_read_that_produces_the_deliverable_still_counts():
    """Deriving RESOLUTION.md FROM a golden is the leak itself."""
    info = scan_commands([_turn([_bash("sed 's/x/y/' ../scen/RESOLUTION.md > RESOLUTION.md")])], SPEC)
    assert info.verdict is IntegrityVerdict.TAINTED


def test_a_read_before_any_creation_is_still_a_leak():
    info = scan_commands([_turn([_bash("cat RESOLUTION.md"), _bash("echo 'd' > RESOLUTION.md")])], SPEC)
    assert info.verdict is IntegrityVerdict.TAINTED


# --------------------------------------------------------------------------
# The regression guard for the truncation trap
# --------------------------------------------------------------------------


def test_match_past_2000_chars_is_still_found():
    """The scan must NOT reuse CommandExecutedChecker's 2000-char ReDoS clip.

    That checker truncates command text at 2000 characters, which is exactly
    where a long `cat` hides: pad the command past the limit and the match is
    invisible to anything that reuses it. This scan reads
    CommandTelemetry.parameters directly and never truncates the haystack.
    """
    from coder_eval.criteria.command_executed import _MAX_PATTERN_SEARCH_LEN

    padding = "# " + ("x" * (_MAX_PATTERN_SEARCH_LEN + 500))
    command = f"echo start\n{padding}\ncat RESOLUTION.md"
    assert len(command) > _MAX_PATTERN_SEARCH_LEN

    info = scan_commands([_turn([_bash(command)])], SPEC)
    assert info.verdict is IntegrityVerdict.TAINTED
    assert len(info.findings) == 1


# --------------------------------------------------------------------------
# Structured (non-Bash) tools
# --------------------------------------------------------------------------


def test_read_tool_on_graded_material_is_tainted():
    info = scan_commands([_turn([_cmd("Read", {"file_path": "/repo/tasks/leaky/RESOLUTION.md"})])], SPEC)
    assert info.verdict is IntegrityVerdict.TAINTED
    assert info.findings[0].tool_name == "Read"
    assert info.findings[0].kind is IntegrityFindingKind.GRADED_READ


def test_glob_listing_graded_material_is_clean():
    info = scan_commands([_turn([_cmd("Glob", {"pattern": "**/RESOLUTION.md"})])], SPEC)
    assert info.verdict is IntegrityVerdict.CLEAN
    assert info.findings == []


def test_grep_files_mode_is_clean_but_content_mode_is_tainted():
    listing = scan_commands([_turn([_cmd("Grep", {"pattern": "cause", "path": "RESOLUTION.md"})])], SPEC)
    assert listing.verdict is IntegrityVerdict.CLEAN

    content = scan_commands(
        [_turn([_cmd("Grep", {"pattern": "cause", "path": "RESOLUTION.md", "output_mode": "content"})])], SPEC
    )
    assert content.verdict is IntegrityVerdict.TAINTED


def test_grep_with_context_flag_is_tainted():
    info = scan_commands([_turn([_cmd("Grep", {"pattern": "cause", "path": "RESOLUTION.md", "-C": 3})])], SPEC)
    assert info.verdict is IntegrityVerdict.TAINTED


@pytest.mark.parametrize("tool_name", ["Edit", "MultiEdit", "NotebookEdit"])
def test_editing_graded_material_is_a_read(tool_name: str):
    """An edit needs the current content to locate what it replaces."""
    info = scan_commands(
        [
            _turn(
                [_cmd(tool_name, {"file_path": "/repo/tasks/leaky/solution.py", "old_string": "a", "new_string": "b"})]
            )
        ],
        SPEC,
    )
    assert info.verdict is IntegrityVerdict.TAINTED
    assert info.findings[0].tool_name == tool_name


@pytest.mark.parametrize(
    ("tool_name", "parameters"),
    [
        pytest.param("Write", {"file_path": "RESOLUTION.md", "content": "my diagnosis"}, id="write-the-deliverable"),
        pytest.param("WebFetch", {"url": "https://docs.example.com/RESOLUTION.md"}, id="webfetch"),
    ],
)
def test_producing_content_is_not_a_read(tool_name: str, parameters: dict):
    """These name what the agent is CREATING; nothing local is opened."""
    info = scan_commands([_turn([_cmd(tool_name, parameters)])], SPEC)
    assert info.verdict is IntegrityVerdict.CLEAN
    assert info.findings == []


@pytest.mark.parametrize(
    ("tool_name", "parameters"),
    [
        pytest.param(
            "Grep",
            {"pattern": "RESOLUTION.md", "path": "src", "output_mode": "content"},
            id="grep-pattern-naming-the-deliverable",
        ),
        pytest.param(
            "Edit",
            {"file_path": "notes.md", "old_string": "see RESOLUTION.md", "new_string": "see report.md"},
            id="edit-prose-naming-the-deliverable",
        ),
        pytest.param(
            "Read",
            {"file_path": "notes.md", "limit": 10, "comment": "compare with RESOLUTION.md later"},
            id="read-with-a-non-path-parameter",
        ),
    ],
)
def test_non_path_parameters_of_known_tools_are_not_matched(tool_name: str, parameters: dict):
    """Patterns and prose NAME graded material without opening it; matching every
    parameter value as if it were a path voids honest rows."""
    info = scan_commands([_turn([_cmd(tool_name, parameters)])], SPEC)
    assert info.verdict is IntegrityVerdict.CLEAN
    assert info.findings == []


def test_a_path_parameter_of_a_known_tool_still_matches():
    info = scan_commands(
        [_turn([_cmd("Grep", {"pattern": "cause", "glob": "RESOLUTION.md", "output_mode": "content"})])], SPEC
    )
    assert info.verdict is IntegrityVerdict.TAINTED


def test_unknown_tool_touching_graded_material_is_inconclusive_not_tainted():
    """We do not guess at an unrecognised tool's semantics in either direction."""
    info = scan_commands([_turn([_cmd("mcp__some__fetch", {"target": "RESOLUTION.md"})])], SPEC)
    assert info.verdict is IntegrityVerdict.INCONCLUSIVE
    assert info.findings == []
    assert any("read semantics are unknown" in n for n in info.notes)


def test_unknown_tool_not_touching_graded_material_is_clean():
    info = scan_commands([_turn([_cmd("mcp__some__fetch", {"target": "notes.md"})])], SPEC)
    assert info.verdict is IntegrityVerdict.CLEAN


# --------------------------------------------------------------------------
# Blind spots
# --------------------------------------------------------------------------


def test_clean_scan_is_clean():
    info = scan_commands([_turn([_bash("ls -la"), _bash("python3 build.py")])], SPEC)
    assert info.verdict is IntegrityVerdict.CLEAN
    assert info.commands_scanned == 2
    assert info.commands_without_parameters == 0


def test_no_commands_is_clean():
    info = scan_commands([_turn([])], SPEC)
    assert info.verdict is IntegrityVerdict.CLEAN
    assert info.commands_scanned == 0


def test_mostly_parameterless_commands_force_inconclusive():
    """Codex returns {} for tool kinds it does not model; a scan that saw almost
    nothing must not report CLEAN."""
    commands = [_cmd("Unknown", {}, tool_id=f"t{i}") for i in range(4)] + [_bash("ls")]
    info = scan_commands([_turn(commands)], SPEC)
    assert info.verdict is IntegrityVerdict.INCONCLUSIVE
    assert info.commands_without_parameters == 4
    assert any("no scannable parameters" in n for n in info.notes)


def test_a_few_parameterless_commands_stay_clean():
    commands = [_bash("ls") for _ in range(20)] + [_cmd("Unknown", {}, tool_id="tX")]
    info = scan_commands([_turn(commands)], SPEC)
    assert info.verdict is IntegrityVerdict.CLEAN


def test_unrecovered_subagents_force_inconclusive():
    info = scan_commands([_turn([_bash("ls")], unrecovered=1)], SPEC)
    assert info.verdict is IntegrityVerdict.INCONCLUSIVE
    assert info.subagent_recovery_incomplete is True


def test_a_hit_beats_every_blind_spot():
    """Going blind cannot un-see a read that WAS observed."""
    commands = [_cmd("Unknown", {}, tool_id=f"t{i}") for i in range(9)] + [_bash("cat RESOLUTION.md")]
    info = scan_commands([_turn(commands, unrecovered=3)], SPEC)
    assert info.verdict is IntegrityVerdict.TAINTED


def test_findings_carry_locating_coordinates():
    turn = _turn([_bash("ls"), _bash("cat RESOLUTION.md")], iteration=2)
    info = scan_commands([turn], SPEC)
    finding = info.findings[0]
    assert finding.iteration == 2
    assert finding.command_index == 1
    assert finding.evidence is not None
    assert "RESOLUTION.md" in finding.evidence


# --------------------------------------------------------------------------
# Graded-material derivation
# --------------------------------------------------------------------------


def _task(**kwargs) -> TaskDefinition:
    base = {
        "task_id": "t",
        "description": "d",
        "initial_prompt": "p",
        "success_criteria": [{"type": "file_exists", "description": "x", "path": "out.txt"}],
    }
    base.update(kwargs)
    return TaskDefinition(**base)


def test_derivation_includes_the_task_file_and_reference():
    task = _task(reference={"file": "solution.py"})
    spec = derive_graded_material(task, Path("/repo/tasks/leaky/task.yaml"))
    assert str(Path("/repo/tasks/leaky/task.yaml")) in spec.paths
    assert str(Path("/repo/tasks/leaky/solution.py")) in spec.paths


def test_derivation_includes_the_reference_directory():
    task = _task(reference={"directory": "_reference"})
    spec = derive_graded_material(task, Path("/repo/tasks/leaky/task.yaml"))
    assert str(Path("/repo/tasks/leaky/_reference")) in spec.directories


def test_derivation_always_includes_the_container_input_mount():
    spec = derive_graded_material(_task(), None)
    assert CONTAINER_INPUT_DIR in spec.directories


def test_derivation_without_a_task_file_keeps_the_globs():
    """A caller that never tracked the YAML still gets location-independent cover."""
    spec = derive_graded_material(_task(), None)
    assert not any("task.yaml" in p for p in spec.paths)
    assert "RESOLUTION.md" in spec.basename_globs


def test_derivation_harvests_task_dir_operands_from_criteria():
    task = _task(
        success_criteria=[
            {
                "type": "run_command",
                "description": "grade",
                "command": "python3 $TASK_DIR/check_answer.py",
            }
        ]
    )
    spec = derive_graded_material(task, None)
    assert "$TASK_DIR/check_answer.py" in spec.paths
    assert "/work/task_dir/check_answer.py" in spec.paths


def test_derivation_harvests_task_dir_operands_from_hooks():
    task = _task(post_run=[{"command": "cp ${TASK_DIR}/expected.json ."}])
    spec = derive_graded_material(task, None)
    assert "$TASK_DIR/expected.json" in spec.paths


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cat check.py", id="bare-check-py"),
        pytest.param("cat check_env.py", id="bare-check-glob"),
        pytest.param("python3 ./check.py", id="own-working-directory"),
        pytest.param("cat src/check.py", id="ordinary-application-code"),
    ],
)
def test_a_grader_glob_needs_a_task_directory_component(command: str):
    """`check.py` is ordinary application code; only its LOCATION makes it a grader."""
    assert _bash_read(command, SPEC) == (False, None)


def test_task_dir_operands_add_the_resolved_spelling():
    task_dir = Path("/repo/tasks/leaky")
    operands = _task_dir_operands("python3 $TASK_DIR/check_x.py", task_dir)
    assert operands == {
        "$TASK_DIR/check_x.py",
        "/work/task_dir/check_x.py",
        str((task_dir / "check_x.py").resolve()),
    }


def test_a_tempdir_run_matches_the_resolved_task_dir_path(tmp_path):
    """Under `driver: tempdir` the agent reads the real checkout path, which
    neither `$TASK_DIR/...` nor `/work/task_dir/...` matches."""
    task_file = tmp_path / "scenario" / "task.yaml"
    task = _task(
        success_criteria=[{"type": "run_command", "description": "grade", "command": "python3 $TASK_DIR/grade_it.py"}]
    )
    spec = derive_graded_material(task, task_file)
    resolved = (task_file.parent / "grade_it.py").resolve()

    info = scan_commands([_turn([_bash(f"cat {resolved.as_posix()}")])], spec)
    assert info.verdict is IntegrityVerdict.TAINTED


def test_derivation_picks_up_declared_mock_dirs():
    """The mock store is declared in `sandbox.mock_path_dirs`, nowhere else."""
    spec = derive_graded_material(_task(sandbox={"mock_path_dirs": ["m"]}), None)
    assert "m" in spec.mock_segments


def test_derivation_picks_up_a_staged_fixture_mount_point():
    task = _task(
        sandbox={"template_sources": [{"type": "template_dir", "path": "fixtures", "mount_point": "stubs/uip"}]}
    )
    spec = derive_graded_material(task, None)
    # Both the declared mount point and its root: the fixtures sit under either.
    assert {"stubs/uip", "stubs"} <= spec.mock_segments


def test_derivation_ignores_a_sandbox_root_mount_point():
    """`mount_point: .` is the whole sandbox; treating it as fixture data would
    make every read of the agent's own work a finding."""
    task = _task(sandbox={"template_sources": [{"type": "template_dir", "path": "starter", "mount_point": "."}]})
    spec = derive_graded_material(task, None)
    assert spec.mock_segments == frozenset({"mocks", "mock_src"})


def test_derivation_always_covers_the_fixture_conventions():
    spec = derive_graded_material(_task(), None)
    assert "_fixtures" in spec.path_segments
    assert {"mocks", "mock_src"} <= spec.mock_segments


# --------------------------------------------------------------------------
# The three measured leak classes the spec used to miss entirely
# --------------------------------------------------------------------------


def test_a_golden_solution_under_fixtures_is_a_graded_read():
    """Another task's `_fixtures/` solution: an answer key, not fixture data."""
    spec = derive_graded_material(_task(), None)
    info = scan_commands([_turn([_bash("cat ../broken-flow/_fixtures/expected/RESOLUTION_body.txt")])], spec)
    assert info.verdict is IntegrityVerdict.TAINTED
    assert info.findings[0].kind is IntegrityFindingKind.GRADED_READ


def test_the_mock_manifest_and_shim_are_mock_data_reads():
    spec = derive_graded_material(_task(sandbox={"mock_path_dirs": ["mocks"]}), None)
    info = scan_commands(
        [_turn([_bash("cat ../mocks/responses/manifest.json"), _bash("sed -n '1,80p' mocks/uip")])],
        spec,
    )
    assert info.verdict is IntegrityVerdict.TAINTED
    assert len(info.findings) == 2
    assert {f.kind for f in info.findings} == {IntegrityFindingKind.MOCK_DATA_READ}


def test_a_sealed_fixture_store_decode_is_flagged():
    """The measured shape: decompress the sealed store in a python one-liner."""
    spec = derive_graded_material(_task(sandbox={"mock_path_dirs": ["m"]}), None)
    command = "python -c \"import base64,zlib;print(zlib.decompress(base64.b64decode(open('m/.store','rb').read())))\""
    info = scan_commands([_turn([_bash(command)])], spec)
    assert info.verdict is IntegrityVerdict.TAINTED
    assert info.findings[0].kind is IntegrityFindingKind.MOCK_DATA_READ


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cat program/main.py", id="segment-inside-another-component"),
        pytest.param("cat streams/m.json", id="segment-as-a-filename"),
        pytest.param("cat my_mocks_notes.md", id="segment-inside-a-basename"),
    ],
)
def test_a_segment_only_matches_a_whole_path_component(command: str):
    spec = derive_graded_material(_task(sandbox={"mock_path_dirs": ["m"]}), None)
    info = scan_commands([_turn([_bash(command)])], spec)
    assert info.verdict is IntegrityVerdict.CLEAN


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python3 $TASK_DIR/check_x.py", {"$TASK_DIR/check_x.py", "/work/task_dir/check_x.py"}),
        ("python3 ${TASK_DIR}/check_x.py", {"$TASK_DIR/check_x.py", "/work/task_dir/check_x.py"}),
        ('cat "$TASK_DIR/a.txt" && ls', {"$TASK_DIR/a.txt", "/work/task_dir/a.txt"}),
        ("echo $TASK_DIR", set()),
        ("no variable here", set()),
    ],
)
def test_task_dir_operand_extraction(command: str, expected: set[str]):
    assert _task_dir_operands(command) == expected


# --------------------------------------------------------------------------
# evaluate_integrity: mode handling and failure containment
# --------------------------------------------------------------------------


def test_mode_off_skips_the_scan():
    info = evaluate_integrity(_task(), None, [_turn([_bash("cat RESOLUTION.md")])], mode=IntegrityMode.OFF)
    assert info.verdict is IntegrityVerdict.SKIPPED
    assert info.mode is IntegrityMode.OFF
    assert info.findings == []


@pytest.mark.parametrize("mode", [IntegrityMode.DETECT, IntegrityMode.VOID])
def test_detect_and_void_both_scan_and_stamp_the_mode(mode: IntegrityMode):
    info = evaluate_integrity(_task(), None, [_turn([_bash("cat RESOLUTION.md")])], mode=mode)
    assert info.verdict is IntegrityVerdict.TAINTED
    assert info.mode is mode
    # The gate, not the scan, decides whether to void.
    assert info.voided is False


def test_a_scan_failure_is_inconclusive_not_a_crash():
    """An integrity bug must not take down a row that otherwise ran fine."""

    class _Exploding(list):
        def __iter__(self):
            raise RuntimeError("boom")

    info = evaluate_integrity(_task(), None, _Exploding(), mode=IntegrityMode.VOID)
    assert info.verdict is IntegrityVerdict.INCONCLUSIVE
    assert any("boom" in n for n in info.notes)


def test_empty_spec_skips_rather_than_reporting_clean():
    """With nothing to match against, CLEAN would be an unearned reassurance."""
    from coder_eval import integrity

    spec = GradedMaterialSpec()
    assert spec.is_empty() is True

    original = integrity.derive_graded_material
    try:
        integrity.derive_graded_material = lambda _task, _file: GradedMaterialSpec()
        info = evaluate_integrity(_task(), None, [_turn([_bash("ls")])], mode=IntegrityMode.DETECT)
    finally:
        integrity.derive_graded_material = original

    assert info.verdict is IntegrityVerdict.SKIPPED
