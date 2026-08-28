"""Reference-solution integrity errors."""


class ReferenceTamperedError(RuntimeError):
    """The staged reference solution changed between staging and grading.

    The anti-cheat permission window spans ``agent.communicate`` only, and the
    docker reference mount must be writable for that window to work at all
    (``chmod`` on a ``:ro`` bind fails with EROFS). So between turns — and after
    the last one — an agent-backgrounded process can write to the reference.
    Overwriting it with the agent's own file would drive ``reference_comparison``
    straight to 1.0.

    Raised by ``Orchestrator._verify_reference_integrity`` when the tree's
    content hash no longer matches the one taken at staging time. It is an
    ERROR, not a failed criterion: the run produced no trustworthy score, and
    booking it as an agent failure would hide a tampering signal inside an
    ordinary pass-rate dip.
    """
