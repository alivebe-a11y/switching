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
    from switching.detectors.analyst_upgrade import AnalystUpgradeDetector
    from switching.detectors.buyback import BuybackDetector
    from switching.detectors.contract_win import ContractWinDetector
    from switching.detectors.crypto_treasury import CryptoTreasuryDetector
    from switching.detectors.dividend_surprise import DividendSurpriseDetector
    from switching.detectors.earnings_surprise import EarningsSurpriseDetector
    from switching.detectors.fda_decision import FdaDecisionDetector
    from switching.detectors.guidance_raise import GuidanceRaiseDetector
    from switching.detectors.index_inclusion import IndexInclusionDetector
    from switching.detectors.insider_cluster import InsiderClusterDetector
    from switching.detectors.mna_target import MnaTargetDetector
    from switching.detectors.spinoff import SpinoffDetector
    from switching.detectors.stock_split import StockSplitDetector

    _REGISTRY[AIPivotDetector.name] = AIPivotDetector
    _REGISTRY[AnalystUpgradeDetector.name] = AnalystUpgradeDetector
    _REGISTRY[BuybackDetector.name] = BuybackDetector
    _REGISTRY[ContractWinDetector.name] = ContractWinDetector
    _REGISTRY[CryptoTreasuryDetector.name] = CryptoTreasuryDetector
    _REGISTRY[DividendSurpriseDetector.name] = DividendSurpriseDetector
    _REGISTRY[Activist13DDetector.name] = Activist13DDetector
    _REGISTRY[EarningsSurpriseDetector.name] = EarningsSurpriseDetector
    _REGISTRY[FdaDecisionDetector.name] = FdaDecisionDetector
    _REGISTRY[GuidanceRaiseDetector.name] = GuidanceRaiseDetector
    _REGISTRY[InsiderClusterDetector.name] = InsiderClusterDetector
    _REGISTRY[IndexInclusionDetector.name] = IndexInclusionDetector
    _REGISTRY[MnaTargetDetector.name] = MnaTargetDetector
    _REGISTRY[SpinoffDetector.name] = SpinoffDetector
    _REGISTRY[StockSplitDetector.name] = StockSplitDetector


def reset() -> None:
    _REGISTRY.clear()


def _register_for_test(cls: type[Detector]) -> type[Detector]:
    """Test helper: register without the duplicate-name guard."""
    _REGISTRY[cls.name] = cls
    return cls
