"""Worker images: Dockerfile generation plus docker build/push and minikube build helpers."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

import sk8s
from sk8s import cluster
from sk8s.config import Config, load_config
from sk8s.errors import Sk8sError

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
    miniforge instead, with Python pinned to the client's minor version and
    sk8s (with the s3/gcs extras) installed from PyPI at the client's exact
    version. An explicit ``base`` cannot be combined with conda deps, since an
    arbitrary base has no conda.
    """
    if not pip and not conda:
        raise ValueError("nothing to build: give at least one pip or conda dependency")
    if base is not None and conda:
        raise ValueError("base cannot be combined with conda deps")

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
        f"RUN pip install --no-cache-dir {_quote([f'sk8s[s3,gcs]=={sk8s.__version__}', *pip])}",
    ]) + "\n"


_STDERR_TAIL_LINES = 50


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _output_tail(stderr: Any, stdout: Any) -> str:
    """One tail of at most ``_STDERR_TAIL_LINES`` lines: stderr first, then stdout.

    Stdout lines only fill whatever room stderr leaves, so a legacy builder
    that prints pip's error on stdout still surfaces it.
    """
    err = _text(stderr).splitlines()[-_STDERR_TAIL_LINES:]
    room = _STDERR_TAIL_LINES - len(err)
    out = _text(stdout).splitlines()[-room:] if room > 0 else []
    return "\n".join([*err, *out])


def _run(runner: Callable[..., Any] | None, argv: list[str], what: str, **kwargs: Any) -> None:
    run = subprocess.run if runner is None else runner
    try:
        proc = run(argv, capture_output=True, text=True, **kwargs)
    except FileNotFoundError as e:
        raise Sk8sError(f"{what} failed: {argv[0]!r} not found on PATH") from e
    if proc.returncode != 0:
        tail = _output_tail(proc.stderr, proc.stdout)
        raise Sk8sError(f"{what} failed (exit {proc.returncode}):\n{tail}")


def push(tag: str, *, runner: Callable[..., Any] | None = None) -> None:
    """``docker push`` the tag; raises Sk8sError with the stderr tail on failure."""
    _run(runner, ["docker", "push", tag], f"docker push {tag}")


_push = push  # ``build`` has a ``push`` parameter that shadows the function


def build(
    tag: str,
    *,
    dockerfile: str | None = None,
    path: str | Path = ".",
    pip: Sequence[str] = (),
    conda: Sequence[str] = (),
    channels: Sequence[str] = (),
    push: bool = False,
    runner: Callable[..., Any] | None = None,
) -> str:
    """Build an image with ``docker build`` and return its tag.

    The Dockerfile (given, or generated from ``pip``/``conda``/``channels``) is
    fed to docker on stdin. With ``push=True`` the image is pushed afterwards.
    An explicit ``dockerfile`` cannot be combined with ``pip``/``conda``/``channels``.
    """
    if dockerfile is not None and (pip or conda or channels):
        raise ValueError("dockerfile cannot be combined with pip/conda/channels")
    if dockerfile is None:
        dockerfile = generate_dockerfile(pip=pip, conda=conda, channels=channels)
    _run(runner, ["docker", "build", "-t", tag, "-f", "-", str(path)],
         f"docker build {tag}", input=dockerfile)
    if push:
        _push(tag, runner=runner)
    return tag


def build_in_minikube(
    tag: str,
    *,
    dockerfile: str | None = None,
    path: str | Path = ".",
    runner: Callable[..., Any] | None = None,
) -> str:
    """Build straight into minikube's image store (no registry round trip).

    ``minikube image build`` cannot read a Dockerfile from stdin, so the text
    goes through a temporary file that is removed afterwards.
    """
    if dockerfile is None:
        raise ValueError("build_in_minikube requires a dockerfile")
    fd, name = tempfile.mkstemp(prefix="sk8s-Dockerfile-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(dockerfile)
        _run(runner, ["minikube", "image", "build", "-t", tag, "-f", name, str(path)],
             f"minikube image build {tag}")
    finally:
        os.unlink(name)
    return tag
