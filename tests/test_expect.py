"""expect= semantics: how an expectation's verdict maps to ExpectationFailed."""
import pytest

from sk8s import expect as qc
from sk8s.errors import ExpectationFailed


def test_none_and_true_pass() -> None:
    assert qc.check(lambda r: None, 1) is None
    assert qc.check(lambda r: True, 1) is None


def test_string_return_fails_with_that_message() -> None:
    v = qc.check(lambda r: f"mapped {r:.0%} < 90%", 0.61)
    assert v is not None
    assert v.error.message == "mapped 61% < 90%"
    assert v.error.result_repr == "0.61"


def test_false_fails_naming_the_expectation() -> None:
    def mapped_over_90(r: float) -> bool:
        return r > 0.9

    v = qc.check(mapped_over_90, 0.5)
    assert v is not None
    assert "mapped_over_90" in v.error.message and "False" in v.error.message


def test_assertion_error_message_is_the_message() -> None:
    def gate(r: int) -> None:
        assert r > 5, f"{r} is not above 5"

    v = qc.check(gate, 3)
    assert v is not None
    assert v.error.message.startswith("3 is not above 5")  # pytest may append assert detail
    assert "AssertionError" in v.tb


def test_bare_assertion_error_gets_a_message() -> None:
    def gate(r: int) -> None:
        raise AssertionError

    v = qc.check(gate, 3)
    assert v is not None
    assert "gate" in v.error.message


def test_other_exception_is_a_failure_with_type_in_message() -> None:
    v = qc.check(lambda r: r["missing"], {})
    assert v is not None
    assert "KeyError" in v.error.message


def test_ambiguous_return_value_fails_rather_than_passing_silently() -> None:
    v = qc.check(lambda r: ["a problem", "another"], 1)
    assert v is not None
    assert "list" in v.error.message and "None, True, False or a str" in v.error.message


def test_falsy_non_bool_fails_too() -> None:
    assert qc.check(lambda r: 0, 1) is not None


def test_numpy_bool_is_accepted() -> None:
    np = pytest.importorskip("numpy")
    assert qc.check(lambda r: np.bool_(True), 1) is None
    assert qc.check(lambda r: np.bool_(False), 1) is not None


def test_non_exception_baseexception_propagates() -> None:
    def gate(r: int) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        qc.check(gate, 1)


def test_result_repr_is_truncated() -> None:
    v = qc.check(lambda r: "bad", "x" * 10_000)
    assert v is not None
    assert len(v.error.result_repr) < 700 and v.error.result_repr.endswith("...")


def test_unrepresentable_result_does_not_break_the_check() -> None:
    class Bad:
        def __repr__(self) -> str:
            raise RuntimeError("no repr")

    v = qc.check(lambda r: "bad", Bad())
    assert v is not None
    assert "unrepresentable" in v.error.result_repr


def test_error_is_expectation_failed() -> None:
    v = qc.check(lambda r: "x", 1)
    assert v is not None and isinstance(v.error, ExpectationFailed)


def test_require_callable() -> None:
    with pytest.raises(TypeError, match="callable"):
        qc.require_callable("not callable")
    qc.require_callable(lambda r: None)
