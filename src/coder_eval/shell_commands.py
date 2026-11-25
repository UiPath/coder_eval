"""Split a recorded shell command string into the simple commands it runs.

Backs ``command_executed`` with ``match_on: simple_commands``: :func:`simple_commands`
returns the argv, quotes resolved, of each simple command the shell would run, so a
pattern can tell a command from a mention of it in a heredoc body, a string or a comment.
Leading wrappers (``timeout``, ``sudo``, ``nohup``, ...) are stripped; assignments and
``env`` stay. ``$(...)``, backticks, ``sh -c`` strings and heredocs fed to a shell are
parsed as commands. Not a shell: nothing is expanded or resolved.

STDLIB-ONLY and pure. Linear in the length of the text: each nesting level re-scans only
its own substring, and nesting is capped at ``_MAX_DEPTH``.

Rationale: .claude/notes/contracts.md § Matching on simple commands
"""

import re
import shlex


SUBST = "__SUBST__"

# A PowerShell / cmd.exe command is not POSIX shell; the caller falls back to raw text.
NON_POSIX_SHELLS = frozenset({"pwsh", "powershell", "cmd"})

_MAX_DEPTH = 6
_SEP_CHARS = ";&|()\n"
# A quoted or escaped operator character is a word character. The scan swaps it for a
# private-use stand-in, so shlex cannot split on it, and the split swaps it back.
_PROTECTED = {ch: chr(0xE000 + k) for k, ch in enumerate(_SEP_CHARS)}
_PROTECT = str.maketrans(_PROTECTED)
_UNPROTECT = str.maketrans({v: k for k, v in _PROTECTED.items()})
_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|(\\?)([A-Za-z0-9_.\-]+))")
_HEREDOC_MARK = re.compile(r"^__HEREDOC_(\d+)__$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
# A redirection operator alone (target in the next token) or with its target attached.
_REDIRECT_OP = re.compile(r"^\d*(>>?|<<<|<>|<|>\|)$")
_REDIRECT_WITH_TARGET = re.compile(r"^\d*(>>?|<<<|<>|<|>\|)(?=.)")
_POSIX_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh"})
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
    return _commands(text, 0)


def _commands(text: str, depth: int) -> list[list[str]]:
    if depth > _MAX_DEPTH:
        raise ShellSplitError("nested deeper than the limit")
    top, substitutions, heredocs = _scan(text)
    result: list[list[str]] = []
    for argv in _split(top):
        heredoc_ids = [int(m.group(1)) for t in argv if (m := _HEREDOC_MARK.match(t))]
        argv = _strip_redirections([t for t in argv if not _HEREDOC_MARK.match(t)])
        argv, scripts, extra = _unwrap(argv)
        for k in heredoc_ids:
            quoted, body = heredocs[k]
            if argv and _is_posix_shell(argv[0]) and _command_flag_index(argv) is None:
                result.extend(_commands(body, depth + 1))  # a heredoc fed to a shell is a script
            elif not quoted:
                for sub in _body_substitutions(body):
                    result.extend(_commands(sub, depth + 1))
        for script in scripts:
            result.extend(_commands(script, depth + 1))
        result.extend(e for e in extra if e)
        if argv:
            result.append(argv)
    for sub in substitutions:
        result.extend(_commands(sub, depth + 1))
    return result


def _scan(text: str) -> tuple[str, list[str], list[tuple[bool, str]]]:
    """Remove comments, lift substitutions out and cut heredoc bodies.

    Returns the remaining text, the substitution bodies, and ``(quoted, body)`` per heredoc.
    Each heredoc operator is replaced by a ``__HEREDOC_<k>__`` word, so the caller knows
    which command it feeds.
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
                " " if nxt == "\n" else _PROTECTED[nxt] if quote is None and nxt in _SEP_CHARS else text[i : i + 2]
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
            out.append(f" __HEREDOC_{len(heredocs) - 1}__ ")
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
            current.append(tok.translate(_UNPROTECT))
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


def _command_flag_index(argv: list[str]) -> int | None:
    """Index of a POSIX shell's ``-c`` option cluster (``-c``, ``-lc``, ``-ic``), or None."""
    for i, tok in enumerate(argv[1:], 1):
        if len(tok) >= 2 and tok[0] == "-" and tok[1] != "-" and tok[1:].isalpha() and "c" in tok[1:]:
            return i
        if not tok.startswith("-"):
            return None
    return None


def _skip_options(argv: list[str], with_argument: frozenset[str]) -> list[str]:
    """``argv`` without its leading options; an option in ``with_argument`` also takes the next token."""
    while argv and argv[0].startswith("-") and argv[0] != "-":
        argv = argv[2:] if argv[0] in with_argument else argv[1:]
    return argv


def _unwrap(argv: list[str]) -> tuple[list[str], list[str], list[list[str]]]:
    """Strip keywords and wrappers. Returns (argv, scripts to parse, extra commands)."""
    scripts: list[str] = []
    extra: list[list[str]] = []
    while argv:
        name = program_name(argv[0])
        if argv[0] in _KEYWORDS:
            argv = argv[1:]
        elif argv[0] in _HEADER_KEYWORDS:
            return [], scripts, extra  # `for x in a b` lists words, it runs nothing
        elif name in _PLAIN_WRAPPERS:
            argv = argv[1:]
        elif name in _FLAG_WRAPPERS:
            argv = _skip_options(argv[1:], _FLAG_WRAPPERS[name])
            if name == "timeout":
                argv = argv[1:]  # the duration
        elif name == "eval":
            scripts.append(" ".join(argv[1:]))
            return [], scripts, extra
        elif _is_posix_shell(argv[0]) and (k := _command_flag_index(argv)) is not None:
            rest = argv[k + 1 :]
            # A recorded argv-joined form (`bash -lc uip flow debug`) arrives unquoted,
            # so more than one word after the flag is read as the script.
            if rest:
                scripts.append(" ".join(rest))
            return [], scripts, extra
        else:
            break
    if argv and program_name(argv[0]) == "find":
        argv, extra = _split_find_exec(argv)
    return argv, scripts, extra


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
            commands.append(_unwrap(argv[i + 1 : j])[0])
            i = j + 1
        else:
            kept.append(argv[i])
            i += 1
    return kept, commands
