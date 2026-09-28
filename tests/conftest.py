from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.web_test_helpers import _shutdown_created_web_apps


@pytest.fixture(autouse=True)
def _shutdown_web_apps() -> Iterator[None]:
    with _shutdown_created_web_apps():
        yield
