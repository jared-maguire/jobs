"""Worker image definitions: Dockerfile generation (build/push live elsewhere)."""
from __future__ import annotations

import shlex
import sys
from typing import Sequence

import sk8s
from sk8s import cluster
from sk8s.config import Config, load_config

CONDA_BASE = "condaforge/miniforge3:latest"


def default_base(config: Config | None = None) -> str:
    """The official worker image for this client; delegates to the cluster's rule."""
    return cluster.default_image(load_config() if config is None else config)


def _quote(items: Sequence[str]) -> str:
    return " ".join(shlex.quote(i) for i in items)


def generate_dockerfile(
    *,
    base: str | None = None,
    pip: Sequence[str] = (),
    conda: Sequence[str] = (),
    channels: Sequence[str] = (),
) -> str:
    """Render a Dockerfile whose image can run ``python -m sk8s.worker``.

    Without conda deps the pip deps layer onto ``base`` (default: the official
    image, which already has sk8s). With conda deps the image is built from
    miniforge instead -- ``base`` is not used, since an arbitrary base has no
    conda -- with Python pinned to the client's minor version and sk8s
    installed from PyPI at the client's exact version.
    """
    if not pip and not conda:
        raise ValueError("nothing to build: give at least one pip or conda dependency")

    if not conda:
        lines = [f"FROM {base or default_base()}"]
        lines.append(f"RUN pip install --no-cache-dir {_quote(pip)}")
        return "\n".join(lines) + "\n"

    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    flags = "".join(f"-c {shlex.quote(c)} " for c in channels)
    return "\n".join([
        f"FROM {CONDA_BASE}",
        f"RUN conda install -y {flags}{_quote([f'python={py}', *conda])}"
        " && conda clean -afy",
        f"RUN pip install --no-cache-dir {_quote([f'sk8s=={sk8s.__version__}', *pip])}",
    ]) + "\n"
