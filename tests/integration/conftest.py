import os

import pytest

requires_cluster = pytest.mark.skipif(
    os.environ.get("SK8S_INTEGRATION") != "1",
    reason="set SK8S_INTEGRATION=1 with a configured cluster + sk8s-dev image",
)


def suite_namespace() -> str:
    """Namespace the suites run in: ``SK8S_NAMESPACE`` (CI: default), else the legacy ``default``."""
    return os.environ.get("SK8S_NAMESPACE") or "default"


def suite_pull_policy() -> str:
    """Worker image pull policy. ``Never`` for images built into minikube; set
    ``SK8S_TEST_PULL_POLICY=IfNotPresent`` for a registry image (e.g. GKE + Artifact Registry)."""
    return os.environ.get("SK8S_TEST_PULL_POLICY") or "Never"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """``SK8S_TEST_TIMEOUT_SCALE=2`` multiplies every per-test timeout mark (scale-from-zero
    node pools add minutes to a cold first pod; minikube's budgets stay the default)."""
    scale = float(os.environ.get("SK8S_TEST_TIMEOUT_SCALE") or 1)
    if scale == 1:
        return
    for item in items:
        mark = item.get_closest_marker("timeout")
        if mark is not None and mark.args:
            item.add_marker(pytest.mark.timeout(mark.args[0] * scale), append=False)


@pytest.fixture(scope="session")
def cluster():  # type: ignore[no-untyped-def]
    from sk8s.cluster import Cluster
    from sk8s.config import load_config

    cfg = load_config(
        # The suites run against the legacy default-namespace RBAC (dev-setup.md); pin it so
        # they don't follow the dedicated "sk8s" namespace default. SK8S_NAMESPACE overrides.
        namespace=suite_namespace(),
        image=os.environ.get("SK8S_TEST_IMAGE", "sk8s-dev:latest"),
        pull_policy=suite_pull_policy(),
        ttl_seconds_after_finished=30,
    )
    c = Cluster(cfg)
    yield c
    c.close()
