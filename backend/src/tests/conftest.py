"""Pytest configuration: ensure ``src`` is importable when running tests from repo root."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_runtime_singletons():
    """Drop the shared LLM/embedder/vector-store caches around every test.

    Each case runs on its own event loop with its own throwaway DB, so a
    cached object built from one test's config must never leak into the next
    (and must never be awaited on a dead loop by a retirement task).
    """
    from src.services import runtime

    runtime.reset_shared()
    yield
    runtime.reset_shared()
