"""Shared fixtures.

The pipeline runs once per test session, offline, and every test reads that
one result. Offline means deterministic, so tests can assert on exact counts
without being flaky.
"""

import os
import sys
from pathlib import Path

import pytest

# LiteLLM otherwise fetches its model price map over the network on import.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PDF = ROOT / "data" / "CARL-01.pdf"


@pytest.fixture(scope="session")
def pdf_path():
    return str(PDF)


@pytest.fixture(scope="session")
def settings():
    from regextract.config import get_settings

    return get_settings(reload=True, output_dir=ROOT / "outputs" / "pytest")


@pytest.fixture(scope="session")
def state(settings):
    from regextract.pipeline.graph import run_pipeline

    return run_pipeline(
        pdf_path=str(PDF), issuer="ESMA", jurisdiction="AE", settings=settings
    )


@pytest.fixture(scope="session")
def items(state):
    return state["items"]


@pytest.fixture(scope="session")
def clauses_by_id(state):
    return {c.id: c for c in state["clauses"]}
