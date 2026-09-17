"""``coder_eval.spi``: the plugin import surface re-exports, unchanged, the objects it names."""

import importlib

import coder_eval.spi as spi


_ORIGINS = (
    "coder_eval.agent",
    "coder_eval.agents._transport",
    "coder_eval.agents.registry",
    "coder_eval.agents.watchdog",
    "coder_eval.errors",
    "coder_eval.models",
    "coder_eval.pricing",
    "coder_eval.streaming.callbacks",
    "coder_eval.streaming.collector",
    "coder_eval.streaming.emitter",
    "coder_eval.streaming.events",
    "coder_eval.timing",
)


def test_spi_version_is_three() -> None:
    assert spi.SPI_VERSION == 3


def test_the_emitter_surface_is_exported() -> None:
    assert {"TurnEmitter", "TurnOutcome", "Generation", "Window", "TimingBasis"} <= set(spi.__all__)
    assert {"run_with_watchdog", "WatchdogFired", "SubprocessJsonlAgent", "JsonlDecoder"} <= set(spi.__all__)


def test_the_stop_channel_is_exported() -> None:
    assert {"StopReason", "end_status_for"} <= set(spi.__all__)


def test_all_is_sorted_and_unique() -> None:
    assert spi.__all__ == sorted(spi.__all__)
    assert len(set(spi.__all__)) == len(spi.__all__)


def test_every_export_is_the_origin_object() -> None:
    origins = [importlib.import_module(name) for name in _ORIGINS]
    for name in spi.__all__:
        if name == "SPI_VERSION":
            continue
        exported = getattr(spi, name)
        assert any(getattr(module, name, None) is exported for module in origins), name
