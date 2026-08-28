"""Regression tests for total failure-path text conversion."""

from watcher.text_safety import exception_text, safe_text


def test_safe_text_preserves_ordinary_text_and_survives_broken_truthiness():
    class Untruthable:
        def __bool__(self):
            raise RuntimeError("broken truth conversion")

        def __str__(self):
            return "diagnostic text"

    assert safe_text("ordinary diagnostic") == "ordinary diagnostic"
    assert safe_text(Untruthable()) == "diagnostic text"


def test_safe_text_and_exception_text_survive_broken_string_conversion():
    class UnprintableError(RuntimeError):
        def __bool__(self):
            raise RuntimeError("broken truth conversion")

        def __str__(self):
            raise RuntimeError("broken text conversion")

    error = UnprintableError("hidden")

    assert safe_text(error) == ""
    assert exception_text(error) == "UnprintableError: "
