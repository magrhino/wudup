from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.web_test_helpers import (
    _file_backed_pending_source_seam,
    _shutdown_created_web_apps,
)


@pytest.fixture(autouse=True)
def _shutdown_web_apps() -> Iterator[None]:
    with _shutdown_created_web_apps():
        yield


@pytest.fixture(autouse=True)
def _file_backed_pending_source() -> Iterator[None]:
    with _file_backed_pending_source_seam():
        yield
