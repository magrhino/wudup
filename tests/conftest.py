from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.web_test_helpers import (
    _file_backed_pending_source_seam,
    _offline_docker_host_resolution,
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


@pytest.fixture(autouse=True)
def _offline_docker_host() -> Iterator[None]:
    with _offline_docker_host_resolution():
        yield
