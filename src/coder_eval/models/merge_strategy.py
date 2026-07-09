"""Per-field merge-strategy marker + reader.

A single declarative mechanism for how a config field merges across the 5
resolution layers (default -> experiment-defaults -> task -> variant -> CLI).
The strategy is stored on the Pydantic ``FieldInfo`` via ``json_schema_extra``
and read back by :func:`merge_strategy_of`.

A field WITHOUT an explicit ``MergeField`` annotation falls back to a
type-aware default (:func:`_default_strategy_for`): a nested ``BaseModel`` or a
free-form ``dict`` field merges ``deep``; a ``list`` or scalar field merges
``replace``. ``MergeField(strategy=...)`` is only for a *deliberate* override of
that default — most commonly a ``list`` that should ``append`` rather than
``replace`` (the one genuinely ambiguous case, enforced by lint rule CE014).
"""

from __future__ import annotations

import types
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo


MERGE_STRATEGY_KEY = "x_merge_strategy"
"""``json_schema_extra`` key under which a field's explicit merge strategy lives."""

APPEND_ORDER_KEY = "x_merge_append_order"
"""``json_schema_extra`` key for an ``append`` field's contribution order."""

MergeStrategy = Literal["deep", "append", "replace"]
"""``deep`` = recurse (per-field for nested models, recursive-dict for free-form
dicts); ``append`` = concatenate lists in layer order; ``replace`` = last layer wins."""

AppendOrder = Literal["forward", "reverse"]
"""For ``append`` fields: ``forward`` (default) concatenates in layer order
(lower precedence first); ``reverse`` puts each higher layer's items FIRST (used
by ``post_run``, where experiment-defaults cleanup must run after the task's)."""


def MergeField(*, strategy: MergeStrategy, append_order: AppendOrder = "forward", **kwargs: Any) -> Any:  # noqa: N802 — Field-style factory
    """A ``Field(...)`` that also records an explicit merge ``strategy``.

    ``strategy`` is REQUIRED — ``MergeField`` exists only to override the
    type-aware default. A field that wants the default uses a plain ``Field``.
    ``append_order`` only applies to ``strategy="append"`` (``"reverse"`` flips
    the contribution order). Any caller-supplied ``json_schema_extra`` is merged,
    not clobbered.
    """
    extra = dict(kwargs.pop("json_schema_extra", {}) or {})
    extra[MERGE_STRATEGY_KEY] = strategy
    if append_order != "forward":
        extra[APPEND_ORDER_KEY] = append_order
    return Field(json_schema_extra=extra, **kwargs)


def append_order_of(field_info: FieldInfo) -> AppendOrder:
    """Return an ``append`` field's contribution order (default ``forward``)."""
    extra = field_info.json_schema_extra
    if isinstance(extra, dict) and APPEND_ORDER_KEY in extra:
        order = extra[APPEND_ORDER_KEY]
        # The value is written only by MergeField (its `append_order` param is
        # typed AppendOrder), but guard a hand-written
        # `Field(json_schema_extra={...})` with a bogus value so it fails loud
        # here instead of silently mis-ordering an append.
        if order not in get_args(AppendOrder):
            raise ValueError(f"invalid append order {order!r}; expected one of {get_args(AppendOrder)}")
        return order  # type: ignore[return-value]  # validated against AppendOrder above
    return "forward"


def merge_strategy_of(field_info: FieldInfo) -> MergeStrategy:
    """Return the field's explicit strategy if annotated, else the type-aware default."""
    extra = field_info.json_schema_extra
    if isinstance(extra, dict) and MERGE_STRATEGY_KEY in extra:
        strategy = extra[MERGE_STRATEGY_KEY]
        # The value is written only by MergeField (its `strategy` param is typed
        # MergeStrategy) and CE014 enforces that every list field declares one,
        # but guard a hand-written `Field(json_schema_extra={...})` with a bogus
        # value so it fails loud here instead of silently falling through to
        # "replace" in the merge engine.
        if strategy not in get_args(MergeStrategy):
            raise ValueError(f"invalid merge strategy {strategy!r}; expected one of {get_args(MergeStrategy)}")
        return strategy  # type: ignore[return-value]  # validated against MergeStrategy above
    return _default_strategy_for(field_info.annotation)


def _default_strategy_for(annotation: Any) -> MergeStrategy:
    """Type-aware default: nested ``BaseModel`` / free-form ``dict`` -> ``deep``;
    ``list`` / scalar -> ``replace``. Unwraps ``Optional`` / ``Annotated`` / union.
    """
    models, is_free_form_dict = classify_annotation(annotation)
    return "deep" if (models or is_free_form_dict) else "replace"


def classify_annotation(annotation: Any) -> tuple[list[type[BaseModel]], bool]:
    """Return ``(nested_model_types, is_free_form_dict)`` for a field annotation.

    Unwraps ``Optional`` / ``Annotated`` / unions and collects directly-nested
    ``BaseModel`` subclasses (so a resolver can descend into them). A ``dict[...]``
    origin sets the free-form flag (descent stops there; any key is accepted).
    ``list[...]`` and scalars contribute neither — they are leaves.
    """
    if annotation is None:
        return [], False

    if hasattr(annotation, "__value__"):  # PEP 695 ``type X = ...`` alias
        annotation = annotation.__value__
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]

    origin = get_origin(annotation)
    args = get_args(annotation) if origin in (Union, types.UnionType) else (annotation,)

    models: list[type[BaseModel]] = []
    is_free_form_dict = False
    for arg in args:
        if hasattr(arg, "__value__"):
            arg = arg.__value__
        if get_origin(arg) is Annotated:
            arg = get_args(arg)[0]
        arg_origin = get_origin(arg)
        if arg_origin is dict or arg is dict:  # parameterized ``dict[...]`` or the bare ``dict``
            is_free_form_dict = True
        elif arg_origin in (Union, types.UnionType):
            sub_models, sub_dict = classify_annotation(arg)
            models.extend(sub_models)
            is_free_form_dict = is_free_form_dict or sub_dict
        elif arg_origin is None and isinstance(arg, type) and issubclass(arg, BaseModel):
            models.append(arg)
        # list / other origins -> leaf, ignored
    return models, is_free_form_dict
