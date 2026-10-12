"""The integration suites' image-selection helper (its fallback chain is what keeps CI green)."""
from tests.integration.suite_image import suite_image


def test_specific_var_wins_then_the_generic_one_then_the_default() -> None:
    both = {"SK8S_X_TEST_IMAGE": "mine:1", "SK8S_TEST_IMAGE": "ci:1"}
    assert suite_image("SK8S_X_TEST_IMAGE", "dflt:1", both) == "mine:1"
    assert suite_image("SK8S_X_TEST_IMAGE", "dflt:1", {"SK8S_TEST_IMAGE": "ci:1"}) == "ci:1"
    assert suite_image("SK8S_X_TEST_IMAGE", "dflt:1", {}) == "dflt:1"


def test_empty_values_count_as_unset() -> None:
    env = {"SK8S_X_TEST_IMAGE": "", "SK8S_TEST_IMAGE": ""}
    assert suite_image("SK8S_X_TEST_IMAGE", "dflt:1", env) == "dflt:1"
    assert suite_image("SK8S_X_TEST_IMAGE", "dflt:1", {"SK8S_X_TEST_IMAGE": "", "SK8S_TEST_IMAGE": "ci:1"}) == "ci:1"


def test_reads_the_process_environment_by_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SK8S_Y_TEST_IMAGE", raising=False)
    monkeypatch.setenv("SK8S_TEST_IMAGE", "ci:2")
    assert suite_image("SK8S_Y_TEST_IMAGE", "dflt") == "ci:2"
