import pytest

from app import usage


@pytest.fixture(autouse=True)
def clean_usage_ledger():
    """The usage ledger is process-global; give every test a fresh one."""
    usage.LEDGER.clear()
    yield
    usage.LEDGER.clear()
