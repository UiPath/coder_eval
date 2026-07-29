"""Typed error for a criterion checker's extension-point contract being misused."""


class CheckerMisuseError(RuntimeError):
    """Raised when a checker's checking surface is invoked in a way its contract
    forbids — e.g. calling the derived sync ``_check_impl`` (``asyncio.run``
    bridge) on an async-only checker from inside a running event loop.

    Deliberately NOT converted to a scored-0.0 ``CriterionResult``:
    ``handle_criterion_errors`` / ``handle_criterion_errors_async`` re-raise it
    (same as ``JudgeInfrastructureError``) so a caller's mistake is a loud crash,
    not a silently wrong score.
    """
