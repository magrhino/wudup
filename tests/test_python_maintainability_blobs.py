"""Committed-blob counting must not retain the contents of the compared trees."""

from __future__ import annotations

import importlib.util
import io
import tracemalloc
from unittest.mock import MagicMock, call

import pytest
from tests.test_python_maintainability import CHECKER, Repository


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("maintainability_blobs", CHECKER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_large_committed_blobs_use_bounded_counting_memory(tmp_path, checker):
    repo = Repository(tmp_path)
    expected = {}
    # Different binary, generated, lock and source blobs; include a very long line.
    for index, path in enumerate(
        ["asset.bin", "webui/dist/app.js", "requirements.txt", "source.py"]
    ):
        content = bytes([index]) * (4 * 1024 * 1024) + b"\nlast line"
        repo.write(path, content)
        expected[repo.git("hash-object", "--", path)] = 2
    head = repo.commit()
    git = checker.Git(tmp_path)
    tree = git.tree(head)
    tracemalloc.start()
    try:
        counts = git.line_counts([tree, tree])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert {oid: counts[oid] for oid in expected} == expected
    # Allow interpreter/process bookkeeping without permitting even one 4 MiB blob.
    assert peak < 2 * 1024 * 1024


@pytest.mark.parametrize("oid_length", [40, 64])
@pytest.mark.parametrize(
    "content,count",
    [
        (b"", 0),
        (b"\n", 1),
        (b"x" * 65536, 1),
        (b"x" * 65535 + b"\n", 1),
        (b"x" * 65536 + b"\n", 1),
        (b"\n" * 65536 + b"tail", 65537),
        (b"\x00\r\n" * 30000, 30000),
    ],
)
def test_streamed_counts_preserve_binary_and_chunk_boundaries(
    checker, oid_length, content, count
):
    oid = "a" * oid_length
    stream = io.BytesIO(
        f"{oid} blob {len(content)}\n".encode() + content + b"\nnext response"
    )
    assert checker.Git.blob_line_count(stream, oid) == count
    assert stream.read() == b"next response"


@pytest.mark.parametrize(
    "response",
    [
        b"",
        b"a" * 40 + b" missing\n",
        b"b" * 40 + b" blob 0\n\n",
        b"a" * 40 + b" tree 0\n\n",
        b"a" * 40 + b" blob -1\n",
        b"a" * 40 + b" blob invalid\n",
        b"a" * 40 + b" blob " + b"1" * 140 + b"\n",
        b"a" * 40 + b" blob 5\nab",
        b"a" * 40 + b" blob 2\nab!",
        b"a" * 40 + b" blob 0\n",
    ],
)
def test_invalid_or_truncated_batch_output_fails_closed(checker, response):
    with pytest.raises(ValueError, match="Git"):
        checker.Git.blob_line_count(io.BytesIO(response), "a" * 40)


def test_short_pipe_reads_preserve_remaining_size_and_final_line(checker):
    class ShortReads(io.BytesIO):
        def read(self, size):
            return super().read(min(size, 3))

    oid = "a" * 40
    stream = ShortReads(f"{oid} blob 9\n".encode() + b"aa\nbb\x00\ncc\n")
    assert checker.Git.blob_line_count(stream, oid) == 3


def test_batch_process_counts_unique_regular_blobs_with_sanitized_environment(
    tmp_path, checker, monkeypatch
):
    oid = "a" * 40
    tree = {
        "source.py": ("100644", oid),
        "same.py": ("100755", oid),
        "link": ("120000", "b" * 40),
    }
    batch = MagicMock()
    batch.__enter__.return_value = batch
    batch.stdout = io.BytesIO(f"{oid} blob 1\nx\n".encode())
    batch.wait.return_value = batch.poll.return_value = 0
    popen = MagicMock(return_value=batch)
    monkeypatch.setattr(checker.subprocess, "Popen", popen)
    monkeypatch.setenv("GIT_DIR", "unexpected-repository")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "never-run")
    assert checker.Git(tmp_path).line_counts([tree, tree]) == {oid: 1}
    assert batch.stdin.write.call_args_list == [call(f"{oid}\n".encode())]
    batch.stdin.close.assert_called_once()
    batch.wait.assert_called_once()
    batch.kill.assert_not_called()
    env = popen.call_args.kwargs["env"]
    assert {key for key in env if key.startswith("GIT_")} == {
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_NO_LAZY_FETCH",
        "GIT_TERMINAL_PROMPT",
        "GIT_OPTIONAL_LOCKS",
    }
    assert env["GIT_NO_LAZY_FETCH"] == env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert popen.call_args.kwargs["stderr"] == checker.subprocess.DEVNULL


@pytest.mark.parametrize("failure", ["truncated", "nonzero", "broken_pipe"])
def test_batch_failure_discards_counts_and_closes_process(
    tmp_path, checker, monkeypatch, failure
):
    oid = "a" * 40
    batch = MagicMock()
    batch.__enter__.return_value = batch
    batch.stdout = io.BytesIO(f"{oid} blob 1\nx\n".encode())
    batch.wait.return_value = 1 if failure == "nonzero" else 0
    batch.poll.return_value = 1 if failure == "nonzero" else None
    if failure == "truncated":
        batch.stdout = io.BytesIO(f"{oid} blob 5\nx".encode())
    elif failure == "broken_pipe":
        batch.stdin.write.side_effect = BrokenPipeError
    monkeypatch.setattr(checker.subprocess, "Popen", MagicMock(return_value=batch))
    with pytest.raises(ValueError, match="Git"):
        checker.Git(tmp_path).line_counts([{"source.py": ("100644", oid)}])
    batch.__exit__.assert_called_once()
    if failure != "nonzero":
        batch.kill.assert_called_once()
