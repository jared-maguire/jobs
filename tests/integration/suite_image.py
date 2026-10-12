"""The ONE way an integration suite picks its worker image.

CI builds a single image (``sk8s-dev-ci:ci``) and exports it as ``SK8S_TEST_IMAGE`` (see
``.github/workflows/ci.yml``). A suite that reads only its own variable (``SK8S_<SUITE>_TEST_IMAGE``)
silently falls back to a locally-built tag that does not exist in CI, and every pod dies with
``ErrImageNeverPull``; that has happened to several suites, each time because nobody remembered to
add the suite's variable to ``ci.yml``. ``suite_image`` makes the fallback structural: a new suite
calls it and needs no ``ci.yml`` change.

    IMAGE = suite_image("SK8S_MYSUITE_TEST_IMAGE", "sk8s-dev-mysuite:latest")

Precedence: the suite's own variable (a developer testing one suite against a specific build), then
``SK8S_TEST_IMAGE`` (CI, or a developer using one image for everything), then the default (the tag
the suite's docstring tells you to ``minikube image build``). Empty values count as unset.
New suites MUST use this instead of ``os.environ.get``; older suites should migrate to it.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

GENERIC_VAR = "SK8S_TEST_IMAGE"


def suite_image(var: str, default: str, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get(var) or env.get(GENERIC_VAR) or default
