"""Split a recorded shell command string into the simple commands it runs.

Backs ``command_executed`` with ``match_on: simple_commands``: :func:`simple_commands`
returns the argv, quotes resolved, of each simple command the shell would run, so a
pattern can tell a command from a mention of it in a heredoc body, a string or a comment.
Leading wrappers (``timeout``, ``sudo``, ``nohup``, ...) are stripped; assignments and
``env`` stay. ``$(...)``, backticks, ``sh -c`` strings and heredocs fed to a shell are
parsed as commands. Not a shell: nothing is expanded or resolved.

STDLIB-ONLY and pure. Linear in the length of the text: each nesting level re-scans only
its own substring, and nesting is capped at ``_MAX_DEPTH``.

Also owns shell identification for ``match_on: command_text`` (:func:`looks_like_shell`,
:func:`command_flag_index`), so the two match modes share one ``-c`` detector.

Rationale: .claude/notes/contracts.md § Matching on simple commands
"""

import re
import shlex


SUBST = "__SUBST__"

# A PowerShell / cmd.exe command is not POSIX shell; the caller falls back to raw text.
NON_POSIX_SHELLS = frozenset({"pwsh", "powershell", "cmd"})

_MAX_DEPTH = 6
_SEP_CHARS = ";&|()\n"
# A quoted or escaped operator or redirection character is a word character. The scan swaps
# it for a private-use stand-in, so shlex cannot split on it and it is not read as a
# redirection; the stand-in is swapped back once redirections are stripped.
_PROTECTED = {ch: chr(0xE000 + k) for k, ch in enumerate(_SEP_CHARS + "<>")}
_MARK = "\ue0ff"
# The stand-ins and the heredoc mark must not come from the text, so a text that holds one
# is not split (see :func:`simple_commands`). Only these: agents do write other private-use
# code points (``\ue200cite\ue202turn1view0\ue201`` citation marks).
_OWN_MARKS = re.compile(f"[{''.join(_PROTECTED.values())}{_MARK}]")
_PROTECT = str.maketrans(_PROTECTED)
_UNPROTECT = str.maketrans({v: k for k, v in _PROTECTED.items()})
_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|(\\?)([A-Za-z0-9_.\-]+))")
_HEREDOC_MARK = re.compile(f"^{_MARK}(\\d+){_MARK}$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
# A redirection operator alone (target in the next token) or with its target attached.
_REDIRECT_OP = re.compile(r"^\d*(>>?|<<<|<>|<|>\|)$")
_REDIRECT_WITH_TARGET = re.compile(r"^\d*(>>?|<<<|<>|<|>\|)(?=.)")
_POSIX_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "yash", "fish"})
_KEYWORDS = frozenset({"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}", "esac"})
_HEADER_KEYWORDS = frozenset({"for", "select", "case"})
_PLAIN_WRAPPERS = frozenset({"nohup", "exec", "command", "builtin"})
# Wrapper -> its options that take a separate argument.
_FLAG_WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "--user", "--group"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "time": frozenset({"-f", "-o"}),
    "xargs": frozenset({"-I", "-n", "-P", "-L", "-d", "-a", "-E", "-s", "--delimiter", "--max-args", "--max-procs"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
}


class ShellSplitError(ValueError):
    """The text is not shell this module can split (an unterminated quote, ``$(`` or backtick)."""


def program_name(arg0: str) -> str:
    """``arg0``'s basename, lowercased and without ``.exe``; ``/`` and ``\\`` both separate."""
    base = re.split(r"[\\/]", arg0)[-1].lower()
    return base.removesuffix(".exe")


def simple_commands(text: str) -> list[list[str]]:
    """The argv of each simple command ``text`` runs, in no particular order.

    Raises :class:`ShellSplitError` when the text cannot be split.
    """
    if _OWN_MARKS.search(text):
        raise ShellSplitError("holds a private-use code point the splitter uses as a mark")
    return _commands(text, 0)


def _commands(text: str, depth: int) -> list[list[str]]:
    if depth > _MAX_DEPTH:
        raise ShellSplitError("nested deeper than the limit")
    top, substitutions, heredocs = _scan(text)
    result: list[list[str]] = []
    for index, argv in enumerate(_split(top)):
        fed = [heredocs[int(m.group(1))] for t in argv if (m := _HEREDOC_MARK.match(t))]
        words = _strip_redirections([t for t in argv if not _HEREDOC_MARK.match(t)])
        argv = [t.translate(_UNPROTECT) for t in words]
        result.extend(_argv_commands(argv, fed, depth, joined=depth == 0 and index == 0))
    for sub in substitutions:
        result.extend(_commands(sub, depth + 1))
    return result


def _argv_commands(argv: list[str], fed: list[tuple[bool, str]], depth: int, *, joined: bool) -> list[list[str]]:
    """The commands one split-out argv runs: itself unwrapped, its scripts, ``find -exec`` actions and fed heredocs.

    ``joined``: the argv starts the recorded text, so a shell ``-c`` may be in the argv-joined form.
    """
    argv, scripts, actions = _unwrap(argv, joined=joined)
    result: list[list[str]] = []
    for quoted, body in fed:
        if _feeds_a_script(argv):
            result.extend(_commands(body, depth + 1))  # a heredoc fed to a shell is a script
        elif not quoted:
            for sub in _body_substitutions(body):
                result.extend(_commands(sub, depth + 1))
    for script in scripts:
        result.extend(_commands(script, depth + 1))
    for action in actions:
        result.extend(_argv_commands(action, [], depth, joined=False))
    if argv:
        result.append(argv)
    return result


def _feeds_a_script(argv: list[str]) -> bool:
    """True if ``argv``'s stdin is a script: its program, after assignments and ``env``, is a shell without ``-c``."""
    program = argv[_assignment_prefix_len(argv) :]
    return bool(program) and _is_posix_shell(program[0]) and command_flag_index(program) is None


def _scan(text: str) -> tuple[str, list[str], list[tuple[bool, str]]]:
    """Remove comments, lift substitutions out and cut heredoc bodies.

    Returns the remaining text, the substitution bodies, and ``(quoted, body)`` per heredoc.
    Each heredoc operator is replaced by a ``<mark>k<mark>`` word (``_MARK``), so the caller
    knows which command it feeds.
    """
    out: list[str] = []
    substitutions: list[str] = []
    heredocs: list[tuple[bool, str]] = []
    pending: list[tuple[int, bool, str]] = []  # (index, strip tabs, delimiter); bodies start after the newline
    i, n = 0, len(text)
    quote: str | None = None
    while i < n:
        c = text[i]
        if quote == "'":
            out.append(_PROTECTED.get(c, c))
            quote = None if c == "'" else quote
            i += 1
        elif c == "\\" and i + 1 < n:
            nxt = text[i + 1]
            out.append(
                " " if nxt == "\n" else _PROTECTED[nxt] if quote is None and nxt in _PROTECTED else text[i : i + 2]
            )
            i += 2
        elif (c == "'" and quote is None) or c == '"':
            quote = None if quote == c else c
            out.append(c)
            i += 1
        elif (sub := _substitution_at(text, i, quote)) is not None:
            i, body, word = sub
            if body is not None:
                substitutions.append(body)
            out.append(word)
        elif quote is not None:
            out.append(_PROTECTED.get(c, c))
            i += 1
        elif text.startswith((">&", "&>"), i):
            out.append(">")
            i += 2
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;&|()"):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("<<", i) and not text.startswith("<<<", i) and (m := _HEREDOC.match(text, i)):
            quoted = m.group(2) is not None or m.group(3) is not None or m.group(4) == "\\"
            delimiter = next(g for g in (m.group(2), m.group(3), m.group(5)) if g is not None)
            pending.append((len(heredocs), m.group(1) == "-", delimiter))
            heredocs.append((quoted, ""))
            out.append(f" {_MARK}{len(heredocs) - 1}{_MARK} ")
            i = m.end()
        elif c == "\n" and pending:
            out.append("\n")
            i = _read_heredoc_bodies(text, i + 1, pending, heredocs)
            pending = []
        else:
            out.append(c)
            i += 1
    if quote is not None:
        raise ShellSplitError("unterminated quote")
    return "".join(out), substitutions, heredocs


def _substitution_at(text: str, i: int, quote: str | None) -> tuple[int, str | None, str] | None:
    """A substitution starting at ``text[i]``: (index after it, body to parse or None, word for the outer command).

    Backticks and ``$(...)`` expand inside double quotes too; ``<(...)`` / ``>(...)`` only
    outside quotes. ``$((...))`` is arithmetic: one word, nothing to parse.
    """
    c = text[i]
    if c == "`":
        j = _closing_backtick(text, i + 1)
        return j + 1, text[i + 1 : j], SUBST
    if c == "$" and text.startswith("((", i + 1):
        j = _closing_paren(text, i + 1)
        return j + 1, None, text[i : j + 1].translate(_PROTECT)
    if (c == "$" or (c in "<>" and quote is None)) and text.startswith("(", i + 1):
        j = _closing_paren(text, i + 1)
        return j + 1, text[i + 2 : j], SUBST
    return None


def _read_heredoc_bodies(
    text: str, i: int, pending: list[tuple[int, bool, str]], heredocs: list[tuple[bool, str]]
) -> int:
    """Read the body of each pending heredoc from ``text[i]`` on, store it, and return the index after the last one."""
    n = len(text)
    for k, strip_tabs, delimiter in pending:
        body: list[str] = []
        while i < n:
            j = text.find("\n", i)
            line = text[i:] if j < 0 else text[i:j]
            i = n if j < 0 else j + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
            body.append(line)
        heredocs[k] = (heredocs[k][0], "\n".join(body))
    return i


def _closing_backtick(text: str, start: int) -> int:
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "`":
            return i
        i += 1
    raise ShellSplitError("unterminated backtick")


def _closing_paren(text: str, open_idx: int) -> int:
    """Index of the ``)`` that closes the ``(`` at ``open_idx``, skipping quoted text."""
    depth = 0
    i = open_idx
    quote: str | None = None
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif c == "\\":
            i += 1
        elif c == "'" and quote is None:
            quote = "'"
        elif c == '"':
            quote = None if quote == '"' else '"'
        elif quote is None and c == "(":
            depth += 1
        elif quote is None and c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ShellSplitError("unbalanced $(")


def _body_substitutions(body: str) -> list[str]:
    """The ``$(...)`` and backtick contents of an unquoted heredoc body: the only part the shell runs."""
    subs: list[str] = []
    i = 0
    try:
        while i < len(body):
            c = body[i]
            if c == "\\":
                i += 2
                continue
            if c == "`":
                j = _closing_backtick(body, i + 1)
                subs.append(body[i + 1 : j])
                i = j + 1
                continue
            if c == "$" and body.startswith("(", i + 1) and not body.startswith("((", i + 1):
                j = _closing_paren(body, i + 1)
                subs.append(body[i + 2 : j])
                i = j + 1
                continue
            i += 1
    except ShellSplitError:
        pass  # prose with a stray backtick or `$(`: keep what was found
    return subs


def _split(top: str) -> list[list[str]]:
    """Tokenize with shell quoting and cut the tokens into commands at the operators."""
    lexer = shlex.shlex(top, posix=True, punctuation_chars=_SEP_CHARS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as e:
        raise ShellSplitError(str(e)) from e
    commands: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok and all(ch in _SEP_CHARS for ch in tok):
            if current:
                commands.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        commands.append(current)
    return commands


def _strip_redirections(argv: list[str]) -> list[str]:
    result: list[str] = []
    skip = False
    for tok in argv:
        if skip:
            skip = False
        elif _REDIRECT_OP.match(tok):
            skip = True
        elif not _REDIRECT_WITH_TARGET.match(tok):
            result.append(tok)
    return result


def _is_posix_shell(arg0: str) -> bool:
    return program_name(arg0) in _POSIX_SHELLS


def looks_like_shell(arg0: str) -> bool:
    """True if ``arg0``'s basename ends in ``sh``: the open shell test of ``match_on: command_text``.

    A predicate, not the :data:`_POSIX_SHELLS` allowlist: there the raw text stays a
    haystack, so recall is safe. Here a shell's argv is replaced by its script, so a
    false shell (``ssh``, ``./publish``) would lose its own command; the allowlist is used.
    """
    return arg0.rsplit("/", 1)[-1].endswith("sh")


def command_flag_index(argv: list[str]) -> int | None:
    """Index of a shell's ``-c`` option cluster in ``argv`` (``-c``, ``-lc``, ``-ic``), or None.

    Options before it are skipped; the first positional word ends the search. ``--long``
    and ``-o=val`` forms are not a ``-c`` cluster.
    """
    for i, tok in enumerate(argv[1:], 1):
        if len(tok) >= 2 and tok[0] == "-" and tok[1] != "-" and tok[1:].isalpha() and "c" in tok[1:]:
            return i
        if not tok.startswith("-"):
            return None
    return None


def _skip_options(argv: list[str], start: int, with_argument: frozenset[str]) -> int:
    """Index of the first non-option word from ``start`` on; an option in ``with_argument`` takes the next word."""
    i = start
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "-":
        i += 2 if argv[i] in with_argument else 1
    return i


def _unwrap(argv: list[str], *, joined: bool) -> tuple[list[str], list[str], list[list[str]]]:
    """Strip keywords and wrappers. Returns (argv, scripts to parse, ``find -exec`` actions).

    With ``joined``, every word after a leading shell's ``-c`` is the script: Codex rollout
    recovery records argv joined without re-quoting (``bash -lc uip flow debug``). Anywhere
    else only the next word is the script, as in the shell (``sh -c 'cmd "$1"' _ {}``).
    """
    scripts: list[str] = []
    extra: list[list[str]] = []
    i = 0  # walk an index and slice once: re-slicing per wrapper is quadratic in the wrapper run
    while i < len(argv):
        name = program_name(argv[i])
        if argv[i] in _KEYWORDS or name in _PLAIN_WRAPPERS:
            i += 1
        elif argv[i] in _HEADER_KEYWORDS:
            return [], scripts, extra  # `for x in a b` lists words, it runs nothing
        elif name in _FLAG_WRAPPERS:
            i = _skip_options(argv, i + 1, _FLAG_WRAPPERS[name])
            if name == "timeout":
                i += 1  # the duration
        elif name == "eval":
            scripts.append(" ".join(argv[i + 1 :]))
            return [], scripts, extra
        elif _is_posix_shell(argv[i]) and (k := command_flag_index(argv[i:])) is not None:
            rest = argv[i + k + 1 :] if joined else argv[i + k + 1 : i + k + 2]
            if rest:
                scripts.append(" ".join(rest))
            return [], scripts, extra
        else:
            break
    argv = argv[i:]
    prefix = _assignment_prefix_len(argv)
    program = argv[prefix:]
    if program and _is_posix_shell(program[0]) and (k := command_flag_index(program)) is not None:
        # `X=1 bash -c '...'`: the assignments stay, the script is parsed.
        scripts.extend(program[k + 1 : k + 2])
        argv = [*argv[: prefix + k + 1], SUBST]
    elif program and program_name(program[0]) == "find":
        kept, extra = _split_find_exec(program)
        argv = argv[:prefix] + kept
    return argv, scripts, extra


def _assignment_prefix_len(argv: list[str]) -> int:
    """How many leading words are assignments or ``env`` with its options and assignments."""
    i = 0
    while i < len(argv):
        if _ASSIGNMENT.match(argv[i]):
            i += 1
        elif program_name(argv[i]) == "env":
            i += 1
            while i < len(argv) and (argv[i].startswith("-") or _ASSIGNMENT.match(argv[i])):
                i += 2 if argv[i] in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string") else 1
        else:
            break
    return i


def _split_find_exec(argv: list[str]) -> tuple[list[str], list[list[str]]]:
    """``find`` without its ``-exec CMD ;`` actions, and each ``CMD`` as a command of its own."""
    kept: list[str] = []
    commands: list[list[str]] = []
    i = 0
    while i < len(argv):
        if argv[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            while j < len(argv) and argv[j] not in (";", "+"):
                j += 1
            commands.append(argv[i + 1 : j])
            i = j + 1
        else:
            kept.append(argv[i])
            i += 1
    return kept, commands
