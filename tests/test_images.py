import subprocess
import sys
from pathlib import Path

import pytest

import sk8s
from sk8s import images
from sk8s.cluster import default_image
from sk8s.config import Config
from sk8s.errors import Sk8sError

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
        pip=["requests"], conda=["samtools", "bwa=0.7"],
        channels=["bioconda", "conda-forge"],
    )
    lines = out.splitlines()
    assert lines[0] == "FROM condaforge/miniforge3:latest"
    assert (
        f"RUN conda install -y -c bioconda -c conda-forge python={PY} samtools bwa=0.7"
        " && conda clean -afy"
    ) in lines
    assert (
        f"RUN pip install --no-cache-dir 'sk8s[s3,gcs]=={sk8s.__version__}' requests"
    ) in lines


def test_base_with_conda_raises():
    with pytest.raises(ValueError, match="base cannot be combined with conda deps"):
        images.generate_dockerfile(base="b:1", conda=["samtools"])


def test_channels_without_conda_raises():
    with pytest.raises(ValueError, match="channels require conda deps"):
        images.generate_dockerfile(base="b:1", pip=["x"], channels=["bioconda"])


def test_conda_without_channels_or_pip():
    out = images.generate_dockerfile(conda=["samtools"])
    assert "-c " not in out
    assert f"conda install -y python={PY} samtools" in out
    assert f"pip install --no-cache-dir 'sk8s[s3,gcs]=={sk8s.__version__}'" in out
    assert out.rstrip().splitlines()[-1].endswith(f"sk8s[s3,gcs]=={sk8s.__version__}'")


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


class FakeRunner:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        self.minikube_file_text = None

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "minikube":
            self.minikube_file_text = Path(argv[argv.index("-f") + 1]).read_text()
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def test_build_exact_argv_and_dockerfile_via_input():
    r = FakeRunner()
    out = images.build("my:tag", dockerfile="FROM x\n", path="ctx", runner=r)
    assert out == "my:tag"
    assert len(r.calls) == 1
    argv, kw = r.calls[0]
    assert argv == ["docker", "build", "-t", "my:tag", "-f", "-", "ctx"]
    assert kw["input"] == "FROM x\n"
    assert kw["capture_output"] is True
    assert kw["text"] is True
    assert not kw.get("shell")


def test_build_generates_dockerfile_from_deps():
    r = FakeRunner()
    images.build("t:1", pip=["requests"], runner=r)
    (argv, kw), = r.calls
    assert kw["input"] == images.generate_dockerfile(pip=["requests"])
    assert argv[-1] == "."


def test_build_hostile_tag_is_one_literal_argv_element():
    r = FakeRunner()
    images.build("$(evil)", dockerfile="FROM x\n", runner=r)
    argv, kw = r.calls[0]
    assert argv[3] == "$(evil)"
    assert argv.count("$(evil)") == 1
    assert not kw.get("shell")


def test_build_failure_raises_with_stderr_tail_capped_at_50_lines():
    stderr = "\n".join(f"line{i}" for i in range(200)) + "\nMARKER-BOOM"
    r = FakeRunner(returncode=1, stderr=stderr)
    with pytest.raises(Sk8sError) as ei:
        images.build("t:1", dockerfile="FROM x\n", runner=r)
    msg = str(ei.value)
    assert "MARKER-BOOM" in msg
    assert "line199" in msg
    assert "line100" not in msg
    assert msg.count("line") <= 50


def test_failure_tail_combines_stderr_then_stdout_capped_at_50_lines():
    stderr = "\n".join(f"err{i}" for i in range(10))
    stdout = "\n".join(f"out{i}" for i in range(200)) + "\nMARKER-PIP"
    r = FakeRunner(returncode=1, stderr=stderr, stdout=stdout)
    with pytest.raises(Sk8sError) as ei:
        images.build("t:1", dockerfile="FROM x\n", runner=r)
    body = str(ei.value).split("\n")[1:]
    assert len(body) == 50
    assert body[:10] == [f"err{i}" for i in range(10)]
    assert body[-1] == "MARKER-PIP"
    assert "out160" not in body and "out161" in body


def test_failure_tail_full_stderr_leaves_no_room_for_stdout():
    stderr = "\n".join(f"err{i}" for i in range(80))
    r = FakeRunner(returncode=1, stderr=stderr, stdout="MARKER-OUT")
    with pytest.raises(Sk8sError) as ei:
        images.build("t:1", dockerfile="FROM x\n", runner=r)
    msg = str(ei.value)
    assert "MARKER-OUT" not in msg
    assert "err79" in msg and "err29" not in msg


def test_build_missing_docker_binary_raises_sk8s_error():
    def runner(argv, **kwargs):
        raise FileNotFoundError(2, "No such file", argv[0])

    with pytest.raises(Sk8sError, match="'docker' not found on PATH"):
        images.build("t:1", dockerfile="FROM x\n", runner=runner)


@pytest.mark.parametrize("kw", [{"pip": ["x"]}, {"conda": ["x"]}, {"channels": ["c"]}])
def test_build_dockerfile_with_generation_args_raises(kw):
    r = FakeRunner()
    with pytest.raises(ValueError, match="dockerfile cannot be combined"):
        images.build("t:1", dockerfile="FROM x\n", runner=r, **kw)
    assert r.calls == []


def test_build_without_push_runs_only_build():
    r = FakeRunner()
    images.build("t:1", dockerfile="FROM x\n", runner=r)
    assert [c[0][1] for c in r.calls] == ["build"]


def test_build_with_push_pushes_after_build():
    r = FakeRunner()
    images.build("t:1", dockerfile="FROM x\n", push=True, runner=r)
    assert [c[0][:2] for c in r.calls] == [["docker", "build"], ["docker", "push"]]
    assert r.calls[1][0] == ["docker", "push", "t:1"]


def test_build_failure_does_not_push():
    r = FakeRunner(returncode=1, stderr="nope")
    with pytest.raises(Sk8sError):
        images.build("t:1", dockerfile="FROM x\n", push=True, runner=r)
    assert len(r.calls) == 1


def test_push_argv_and_failure():
    r = FakeRunner()
    images.push("$(evil)", runner=r)
    assert r.calls[0][0] == ["docker", "push", "$(evil)"]
    bad = FakeRunner(returncode=1, stderr="denied: MARKER-PUSH")
    with pytest.raises(Sk8sError, match="MARKER-PUSH"):
        images.push("t:1", runner=bad)


def test_build_in_minikube_uses_tempfile_and_cleans_up():
    r = FakeRunner()
    out = images.build_in_minikube("$(evil)", dockerfile="FROM y\n", path="ctx", runner=r)
    assert out == "$(evil)"
    argv, kw = r.calls[0]
    assert argv[:4] == ["minikube", "image", "build", "-t"]
    assert argv[4] == "$(evil)"
    assert argv[5] == "-f"
    assert argv[7] == "ctx"
    assert len(argv) == 8
    assert "input" not in kw
    assert r.minikube_file_text == "FROM y\n"
    assert not Path(argv[6]).exists()


def test_build_in_minikube_failure_raises_and_cleans_up():
    r = FakeRunner(returncode=2, stderr="MARKER-MK")
    with pytest.raises(Sk8sError, match="MARKER-MK"):
        images.build_in_minikube("t:1", dockerfile="FROM y\n", runner=r)
    assert not Path(r.calls[0][0][6]).exists()


def test_build_in_minikube_requires_dockerfile_or_deps_default():
    r = FakeRunner()
    with pytest.raises(ValueError):
        images.build_in_minikube("t:1", runner=r)
