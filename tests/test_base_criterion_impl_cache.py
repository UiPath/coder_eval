"""Tests for ``BaseCriterion._impl_accepted_params`` cache invalidation."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from coder_eval.criteria.file_exists import FileExistsChecker
from coder_eval.models import CriterionResult, FileExistsCriterion


def test_impl_accepted_params_cache_invalidates_on_monkeypatch() -> None:
    """Replacing ``_check_impl`` on the subclass refreshes the param cache.

    Regression for finding #7: ``_impl_accepted_params`` was filled once per
    class and never invalidated. A test that monkey-patched ``_check_impl`` to
    accept a new kwarg would have its kwarg silently filtered out because the
    cache reflected the original signature.

    We monkey-patch the class's ``_check_impl`` with a function that accepts
    a new ``reference_dir`` kwarg (which the original FileExistsChecker
    signature does not declare). After the first call to ``check()`` (which
    fills the cache from the original signature), we swap in the new impl and
    confirm the cache refreshes — the new kwarg is forwarded successfully.
    """
    # Reset the class-level cache so a previous test didn't poison state.
    FileExistsChecker._impl_accepted_params = None
    FileExistsChecker._impl_signature_owner = None

    sandbox = MagicMock()
    sandbox.file_exists.return_value = True
    criterion = FileExistsCriterion(path="x.py", description="d")

    # Prime the cache with the original signature.
    checker = FileExistsChecker()
    checker.check(criterion, sandbox)
    cached = FileExistsChecker._impl_accepted_params
    assert cached is not None
    assert "reference_dir" not in cached

    # Monkey-patch _check_impl with a new function that declares reference_dir.
    captured: dict[str, Any] = {}

    def patched_impl(
        self: FileExistsChecker,
        criterion: FileExistsCriterion,
        sandbox: Any,
        reference_code: str | None = None,
        turn_records: list[Any] | None = None,
        route: Any | None = None,
        reference_dir: Any | None = None,
    ) -> CriterionResult:
        captured["reference_dir"] = reference_dir
        return CriterionResult(criterion_type="file_exists", description=criterion.description, score=1.0)

    original = FileExistsChecker._check_impl
    try:
        FileExistsChecker._check_impl = patched_impl  # type: ignore[method-assign]
        checker.check(criterion, sandbox, reference_dir="REF_DIR")  # type: ignore[arg-type]
    finally:
        FileExistsChecker._check_impl = original  # type: ignore[method-assign]
        # Reset cache so subsequent tests aren't affected.
        FileExistsChecker._impl_accepted_params = None
        FileExistsChecker._impl_signature_owner = None

    # The cache refreshed: reference_dir was forwarded to the patched impl.
    assert captured["reference_dir"] == "REF_DIR"
