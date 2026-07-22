"""Harness: no public numeric helper in reports_stats may launder NaN/inf into a report.

This enumerates the module rather than listing functions by hand, so a helper added
later is covered automatically — the failure mode this guards (a new statistic that
silently returns NaN, which then renders as a real-looking number) is exactly the one
that motivated the exact-t work. See review-rubric.md criterion 15.

To add a helper that legitimately propagates non-finite input, add it to
``_PASSTHROUGH_HELPERS`` with a reason — that edit is the point, it forces the call
to be deliberate.
"""

import inspect
import math

import pytest

from coder_eval import reports_stats


# Thin wrappers over the stdlib that intentionally propagate whatever they are given;
# they are inputs to the guarded functions below, not report-facing statistics.
_PASSTHROUGH_HELPERS = {"mean", "stddev"}

_NAN = float("nan")
_INF = float("inf")


def _numeric_helpers():
    """Public reports_stats functions whose parameters are all floats / float lists."""
    for name, fn in vars(reports_stats).items():
        if name.startswith("_") or name in _PASSTHROUGH_HELPERS or not inspect.isfunction(fn):
            continue
        if fn.__module__ != reports_stats.__name__:
            continue
        hints = inspect.get_annotations(fn, eval_str=True)
        params = inspect.signature(fn).parameters
        annotations = [hints.get(p) for p in params]
        if annotations and all(ann is float or ann == list[float] for ann in annotations):
            yield name, fn, params, hints


def _bad_args(params, hints, bad_value):
    args = []
    for name in params:
        args.append([1.0, bad_value] if hints.get(name) == list[float] else bad_value)
    return args


def _floats_in(value):
    if isinstance(value, float):
        yield value
    elif isinstance(value, tuple):
        for item in value:
            yield from _floats_in(item)


@pytest.mark.parametrize("bad_value", [_NAN, _INF, -_INF], ids=["nan", "inf", "-inf"])
def test_no_public_numeric_helper_returns_nan(bad_value):
    """Non-finite input must yield None, a finite number, or an explicit ValueError."""
    checked = []
    for name, fn, params, hints in _numeric_helpers():
        checked.append(name)
        try:
            result = fn(*_bad_args(params, hints, bad_value))
        except ValueError:
            continue  # refusing explicitly is a valid response
        for f in _floats_in(result):
            assert not math.isnan(f), f"{name} returned NaN for {bad_value} input"

    # Guard the guard: if the discovery filter silently matches nothing, the test
    # above would pass vacuously.
    assert {"welch_t_test", "paired_t_test", "paired_t_ci", "student_t_two_tailed_p"} <= set(checked)
