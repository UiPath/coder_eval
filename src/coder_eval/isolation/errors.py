"""Errors raised by the docker isolation layer."""


class DockerRunError(RuntimeError):
    """Raised when ``docker run`` exits non-zero AND no task.json was produced.

    Criterion failures do NOT raise this -- the container always writes
    task.json (with whatever results it has) before exiting, and the host
    parses that regardless of exit code. This is reserved for setup-time
    failures: missing image, daemon down, OOM-kill before the agent started,
    etc.
    """


class EgressSetupError(DockerRunError):
    """Raised when the ``network: llm_only`` egress sidecar cannot be set up.

    The task container never started, so there is no task.json; the runner
    writes a synthetic ERROR record before re-raising.
    """
