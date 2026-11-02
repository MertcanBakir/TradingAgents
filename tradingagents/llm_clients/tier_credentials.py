"""Trusted per-execution secrets, never part of graph config or reports."""

from contextlib import contextmanager
from contextvars import ContextVar

_credentials = ContextVar("tradingagents_tier_credentials", default=None)


@contextmanager
def tier_credentials(credentials):
    token = _credentials.set(dict(credentials))
    try:
        yield
    finally:
        _credentials.reset(token)


def credential_for(tier, provider):
    credentials = _credentials.get()
    if credentials is None:
        return None
    # In hosted API execution, missing credentials must never fall back to env.
    try:
        return credentials[(tier, provider)]
    except KeyError:
        raise ValueError("Missing trusted model credential") from None
