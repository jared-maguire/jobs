# sk8s v2 Plan 6: Images — Builders and the Release Pipeline

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A production worker Dockerfile; `sk8s.images` for generating/building/pushing user images layered on the official base; a release workflow publishing version-locked images per Python minor plus the PyPI package.

**Architecture:** `images.py` generates Dockerfiles as strings and shells (argv-only) to the docker/minikube CLIs — developer tooling, the one sanctioned subprocess surface. The release pipeline is GitHub Actions authored here, exercised in Plan 8.

**Tech Stack:** docker CLI, minikube CLI (dev), GitHub Actions (ghcr + PyPI trusted publishing).

**Spec:** `docs/superpowers/specs/2026-10-10-sk8s-v2-design.md` §8 + §11.

**Branch/workflow:** `feat/images` from `version-2`, clone `~/code/jobs/jobs.images`. PR into `version-2`.

## Global Constraints

(Plan 1's carry over; specifically:) no `shell=True` anywhere — docker/minikube invocations are argv lists; mypy --strict; TDD; suite green per commit.

- Default base image string must equal `cluster.default_image(config)`'s value for the running interpreter (import and reuse it — do not duplicate the format).
- Generated Dockerfiles must produce a worker that can run `python -m sk8s.worker`: i.e. they MUST layer on a base that already contains sk8s, or install sk8s themselves. pip-only path: `FROM <base>` + `RUN pip install --no-cache-dir <pip deps>`. conda path: `FROM condaforge/miniforge3:latest`, `RUN conda install -y python=<minor> <conda deps> -c <channels...>`, then `RUN pip install --no-cache-dir sk8s==<version> <pip deps>`.
- Build failures raise `Sk8sError` carrying the last 50 lines of stderr.
- The release workflow must pin action versions by major tag, push images tagged `ghcr.io/jared-maguire/sk8s:<version>-py<minor>` for minors 3.10–3.13 (+3.14), and use PyPI trusted publishing (no stored API token). No secrets in the workflow file.

## Review Focus

1. **Generated Dockerfile actually runs the worker:** a pip-layer image on the official base keeps `python -m sk8s.worker` working; the conda path installs sk8s explicitly — pinned by generation tests asserting the install lines in Task 1.
2. **argv injection:** tags/paths with spaces or metacharacters pass through subprocess argv untouched (no shell) — pinned in Task 2 with a recording fake.
3. **stderr surfacing:** a failing build raises with the stderr tail, not a bare CalledProcessError — Task 2.
4. **Version lock:** the workflow derives the image tag from the pushed git tag and the package version, and they must match (a guard step fails the job on mismatch) — Task 3.
5. **Matrix completeness:** every supported minor in `pyproject.toml`'s classifiers/requires-python gets an image — Task 3 (reviewer cross-checks).

---

### Task 1: Dockerfiles and generation

**Files:**
- Create: `docker/Dockerfile.worker`, `sk8s/images.py` (generation half), `tests/test_images.py`

**Interfaces:**
```python
# sk8s/images.py
def default_base(config: Config | None = None) -> str          # delegates to cluster.default_image
def generate_dockerfile(*, base: str | None = None, pip: Sequence[str] = (),
                        conda: Sequence[str] = (), channels: Sequence[str] = ()) -> str
```
`docker/Dockerfile.worker` (the official base, built by CI and by `sk8s image` later):
```dockerfile
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY sk8s ./sk8s
RUN pip install --no-cache-dir ".[s3,gcs]"
```
Generation rules: no conda deps → `FROM {base or default_base()}` + one `RUN pip install` line when pip deps exist; conda deps → the miniforge recipe above with `-c` flags per channel and `sk8s=={__version__}` in the pip line. Raise ValueError when both lists are empty (nothing to build).

- [ ] **Step 1:** Failing tests: default base delegation; pip-only output (exact FROM + RUN lines); conda output includes python pin, channels, sk8s version pin; empty-input ValueError; Dockerfile.worker parses (string checks: ARG, slim base, extras install).
- [ ] **Steps 2–5:** fail → implement → suite+mypy → commit `feat: dockerfile generation and worker image definition`.

### Task 2: build / push / minikube load

**Files:** Modify `sk8s/images.py`; extend `tests/test_images.py`.

**Interfaces:**
```python
def build(tag: str, *, dockerfile: str | None = None, path: str | Path = ".",
          pip: Sequence[str] = (), conda: Sequence[str] = (), channels: Sequence[str] = (),
          push: bool = False, runner: Callable[..., Any] | None = None) -> str   # returns tag
def push(tag: str, *, runner=None) -> None
def build_in_minikube(tag: str, *, dockerfile: str | None = None, path: str | Path = ".", runner=None) -> str
```
`runner` defaults to `subprocess.run`; injected in tests. `build`: dockerfile string (generated from pip/conda when not given) fed via `input=` with `["docker", "build", "-t", tag, "-f", "-", str(path)]`, `capture_output=True`; non-zero → `Sk8sError` with stderr tail (≤50 lines). `build_in_minikube` writes the dockerfile to a tempfile (minikube can't read stdin) and runs `["minikube", "image", "build", "-t", tag, "-f", <file>, str(path)]`.

- [ ] **Step 1:** Failing tests with a recording fake runner: exact argv for build/push/minikube (including a tag containing `$(evil)` passing through literally as one argv element); dockerfile routed via input/tempfile; failure → Sk8sError containing a marker string from fake stderr; push only when asked.
- [ ] **Steps 2–5:** fail → implement → suite+mypy → commit `feat: image build/push helpers`. Then a REAL smoke check (not committed as a test): `python -c` build of the pip-only generated dockerfile against the local docker daemon with tag `sk8s-plan6-smoke` — document the output in your report, then `docker rmi` it.

### Task 3: Release workflow

**Files:** Create `.github/workflows/release.yml`; modify `pyproject.toml` only if trusted publishing needs metadata.

Triggers on tag push `v2*`. Jobs: (1) `publish-images`: matrix python-version [3.10, 3.11, 3.12, 3.13, 3.14] → docker/login-action@v3 to ghcr with GITHUB_TOKEN, docker/build-push-action@v6 with `file: docker/Dockerfile.worker`, `build-args: PYTHON_VERSION=<minor>`, `tags: ghcr.io/jared-maguire/sk8s:${VERSION}-py<minor>` where VERSION is the tag minus `v`; a preceding guard step asserts `python -c "import sk8s; print(sk8s.__version__)"` equals VERSION and fails otherwise. (2) `publish-pypi`: build sdist/wheel (`python -m build`), publish via pypa/gh-action-pypi-publish@release/v1 with trusted publishing (id-token: write). No secrets beyond GITHUB_TOKEN.

- [ ] **Step 1:** Write the workflow; validate with `python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('.github/workflows/release.yml').read_text())"` and a self-review against the Review Focus items (no runnable CI here — Plan 8 wires and exercises CI).
- [ ] **Step 2:** Full suite + mypy still green. **Step 3:** Commit `feat: release workflow (ghcr images + PyPI)`.

## Done criteria
Unit + mypy green; smoke build succeeded locally; workflow YAML valid; PR into `version-2`.
