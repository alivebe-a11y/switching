from datetime import datetime

import pytest

from switching import registry
from switching.detectors.base import Detector


class _Dummy(Detector):
    name = "dummy_test"
    description = "test-only detector"

    def scan(self, since: datetime):
        return []


def setup_function(_):
    registry.reset()


def test_register_and_get():
    registry.register(_Dummy)
    assert registry.get("dummy_test") is _Dummy


def test_duplicate_name_raises():
    registry.register(_Dummy)

    class _Other(Detector):
        name = "dummy_test"
        description = "conflicts"

        def scan(self, since):
            return []

    with pytest.raises(ValueError):
        registry.register(_Other)


def test_unknown_name_raises():
    with pytest.raises(KeyError):
        registry.get("does_not_exist")


def test_load_builtin_registers_ai_pivot():
    registry.load_builtin_detectors()
    assert "ai_pivot" in registry.all_detectors()
