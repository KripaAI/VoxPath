"""Tracing is optional: off by default, and a missing or broken setup never breaks the app."""

import builtins
import contextlib

import pytest

import tracing


@pytest.fixture(autouse=True)
def _tracing_off_between_tests(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    yield


def test_tracing_is_off_by_default(monkeypatch):
    monkeypatch.delenv("PHOENIX_TRACING", raising=False)
    assert tracing.setup_tracing() is False
    assert isinstance(tracing.trace_context("t-1", "u-1"), contextlib.nullcontext)


def test_missing_tracing_packages_do_not_break_startup(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    real_import = builtins.__import__

    def no_phoenix(name, *args, **kwargs):
        if name.startswith(("phoenix", "openinference")):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_phoenix)
    assert tracing.setup_tracing() is False
    assert isinstance(tracing.trace_context("t-1", "u-1"), contextlib.nullcontext)


def test_failed_tracing_setup_does_not_break_startup(monkeypatch):
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    phoenix_otel = pytest.importorskip("phoenix.otel")  # only with requirements-tracing.txt

    def broken_register(**kwargs):
        raise RuntimeError("collector misconfigured")

    monkeypatch.setattr(phoenix_otel, "register", broken_register)
    assert tracing.setup_tracing() is False


def test_trace_context_tags_session_and_user_when_on(monkeypatch):
    instrumentation = pytest.importorskip("openinference.instrumentation")  # only with requirements-tracing.txt
    monkeypatch.setattr(tracing, "_enabled", True)

    with tracing.trace_context("thread-9", "user-7"):
        attrs = dict(instrumentation.get_attributes_from_context())
    assert attrs["session.id"] == "thread-9"
    assert attrs["user.id"] == "user-7"
