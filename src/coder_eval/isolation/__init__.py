"""Per-task Docker isolation.

The host dispatches each task as ``docker run --rm coder-eval-agent ...``.
Inside the container, the same Orchestrator/Sandbox/Agent code runs in-process
with ``driver: tempdir``, writes ``task.json``, and exits. The host reads
``task.json`` from a bind-mounted output dir and feeds it to the normal
aggregation pipeline.

Aggregation (P/R/F1, suite thresholds, reports) always stays on the host.
"""

from .docker_runner import DEFAULT_IMAGE_TAG, DockerRunner, default_image_tag


__all__ = ["DEFAULT_IMAGE_TAG", "DockerRunner", "default_image_tag"]
