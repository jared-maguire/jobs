import sys
from pathlib import Path

import pytest

import sk8s
from sk8s import images
from sk8s.cluster import default_image
from sk8s.config import Config

REPO = Path(__file__).resolve().parent.parent
PY = f"{sys.version_info.major}.{sys.version_info.minor}"


def test_default_base_delegates_to_cluster_default_image():
    cfg = Config(image_prefix="reg.example/")
    assert images.default_base(cfg) == default_image(cfg)
    assert images.default_base(Config(image="mine:1")) == "mine:1"


def test_default_base_none_loads_config(monkeypatch):
    monkeypatch.setattr(images, "load_config", lambda: Config(image="loaded:2"))
    assert images.default_base() == "loaded:2"


def test_pip_only_exact_output():
    out = images.generate_dockerfile(base="b:1", pip=["requests", "numpy>=1.26"])
    assert out.splitlines() == [
        "FROM b:1",
        "RUN pip install --no-cache-dir requests 'numpy>=1.26'",
    ]


def test_pip_only_uses_default_base_when_none(monkeypatch):
    monkeypatch.setattr(images, "default_base", lambda config=None: "dflt:9")
    out = images.generate_dockerfile(pip=["requests"])
    assert out.splitlines()[0] == "FROM dflt:9"


def test_conda_output():
    out = images.generate_dockerfile(
        base="ignored:1", pip=["requests"], conda=["samtools", "bwa=0.7"],
        channels=["bioconda", "conda-forge"],
    )
    lines = out.splitlines()
    assert lines[0] == "FROM condaforge/miniforge3:latest"
    assert (
        f"RUN conda install -y -c bioconda -c conda-forge python={PY} samtools bwa=0.7"
        " && conda clean -afy"
    ) in lines
    assert (
        f"RUN pip install --no-cache-dir sk8s=={sk8s.__version__} requests"
    ) in lines
    assert "ignored:1" not in out


def test_conda_without_channels_or_pip():
    out = images.generate_dockerfile(conda=["samtools"])
    assert "-c " not in out
    assert f"conda install -y python={PY} samtools" in out
    assert f"pip install --no-cache-dir sk8s=={sk8s.__version__}" in out
    assert out.rstrip().splitlines()[-1].endswith(f"sk8s=={sk8s.__version__}")


def test_conda_spec_with_shell_metacharacters_is_quoted():
    out = images.generate_dockerfile(conda=["numpy>=1.2", "x; rm -rf /"])
    assert "'numpy>=1.2'" in out
    assert "'x; rm -rf /'" in out


def test_empty_inputs_raise():
    with pytest.raises(ValueError):
        images.generate_dockerfile()
    with pytest.raises(ValueError):
        images.generate_dockerfile(base="b:1", channels=["conda-forge"])


def test_dockerfile_worker_definition():
    text = (REPO / "docker" / "Dockerfile.worker").read_text()
    lines = text.splitlines()
    assert lines[0] == "ARG PYTHON_VERSION=3.12"
    assert "FROM python:${PYTHON_VERSION}-slim" in lines
    assert 'RUN pip install --no-cache-dir ".[s3,gcs]"' in lines
    assert "COPY sk8s ./sk8s" in lines
