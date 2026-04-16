from __future__ import annotations

from typing import Callable, TypeVar

from switching.detectors.base import Detector

T = TypeVar("T", bound=type[Detector])

_REGISTRY: dict[str, type[Detector]] = {}


def register(cls: T) -> T:
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(f"{cls.__name__} must define a non-empty 'name' attribute")
    if name in _REGISTRY and _REGISTRY[name] is not cls:
        raise ValueError(f"detector name already registered: {name}")
    _REGISTRY[name] = cls
    return cls


def get(name: str) -> type[Detector]:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"unknown detector: {name}. Available: {sorted(_REGISTRY)}") from exc


def all_detectors() -> dict[str, type[Detector]]:
    return dict(_REGISTRY)


def load_builtin_detectors() -> None:
    # Import ensures @register side-effects have run, and explicitly re-
    # registers in case the registry was reset (test helper) after first import.
    from switching.detectors.activist_13d import Activist13DDetector
    from switching.detectors.ai_pivot import AIPivotDetector
    from switching.detectors.buyback import BuybackDetector
    from switching.detectors.insider_cluster import InsiderClusterDetector

    _REGISTRY[AIPivotDetector.name] = AIPivotDetector
    _REGISTRY[BuybackDetector.name] = BuybackDetector
    _REGISTRY[Activist13DDetector.name] = Activist13DDetector
    _REGISTRY[InsiderClusterDetector.name] = InsiderClusterDetector


def reset() -> None:
    _REGISTRY.clear()


def _register_for_test(cls: type[Detector]) -> type[Detector]:
    """Test helper: register without the duplicate-name guard."""
    _REGISTRY[cls.name] = cls
    return cls
