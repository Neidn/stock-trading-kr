"""Load strategies by module name from ``src/signal/strategies/``."""

from __future__ import annotations

import importlib
import inspect

from src.signal.base_strategy import BaseStrategy


def load_strategy(name: str, params: dict | None = None) -> BaseStrategy:
    module = importlib.import_module(f"src.signal.strategies.{name}")
    classes = [
        obj for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, BaseStrategy) and obj is not BaseStrategy
        and obj.__module__ == module.__name__
    ]
    if len(classes) != 1:
        raise ImportError(f"{name}: expected exactly one BaseStrategy subclass, found {len(classes)}")
    strategy = classes[0](params or {})
    if strategy.get_name() != name:
        raise ImportError(f"{name}: get_name() returned {strategy.get_name()!r}")
    return strategy


def load_strategies(names: tuple[str, ...], params: dict) -> list[BaseStrategy]:
    return [load_strategy(n, params.get(n, {})) for n in names]
