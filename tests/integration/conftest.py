import os

import pytest

requires_cluster = pytest.mark.skipif(
    os.environ.get("SK8S_INTEGRATION") != "1",
    reason="set SK8S_INTEGRATION=1 with a configured cluster + sk8s-dev image",
)


@pytest.fixture(scope="session")
def cluster():  # type: ignore[no-untyped-def]
    from sk8s.cluster import Cluster
    from sk8s.config import load_config

    cfg = load_config(
        # The suites run against the legacy default-namespace RBAC (dev-setup.md); pin it so
        # they don't follow the dedicated "sk8s" namespace default.
        namespace="default",
        image=os.environ.get("SK8S_TEST_IMAGE", "sk8s-dev:latest"),
        pull_policy="Never",
        ttl_seconds_after_finished=30,
    )
    c = Cluster(cfg)
    yield c
    c.close()
