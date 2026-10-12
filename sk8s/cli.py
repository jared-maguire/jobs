"""``sk8s`` console entrypoint: cluster setup, health checks, images, and workflow housekeeping.

Exit codes: 0 success, 1 failure (message on stderr, never a traceback), 2 usage error (argparse).
``sk8s shell`` is the one place sk8s runs ``kubectl`` (an interactive exec, which needs a TTY);
it lives here and never in the library.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from kubernetes import client
from kubernetes.client import ApiException
from kubernetes.config import ConfigException
from urllib3.exceptions import HTTPError

from sk8s import __version__, anchors, cleaning, datadescribe, explaining, images, k8s, spec, workflows
from sk8s import config as config_mod
from sk8s import doctor as doctor_mod
from sk8s import profile as profile_mod
from sk8s.cluster import job_phase, resolve_image
from sk8s.config import Config, load_config
from sk8s.errors import Sk8sError
from sk8s.store import get_store

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

DEFAULT_BUCKET_ROLE = "roles/storage.objectAdmin"
SHELL_DEADLINE_SECONDS = 4 * 3600  # a shell job outlives its exec (we exec away), so bound it
SHELL_READY_TIMEOUT = 120.0
# Keep the shell pod alive with plain POSIX sh so any image works (no python needed). `sleep
# infinity` is GNU-only, so use a huge number of seconds. PID 1 ignores SIGTERM without a handler,
# so trap it and `wait` on the backgrounded sleep: the pod then terminates promptly, not after the
# 30s grace period.
_SHELL_HANG = "trap 'exit 0' TERM INT; sleep 2147483647 & wait"
_POD_FATAL_WAITING = {
    "ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull",
    "CreateContainerConfigError", "CrashLoopBackOff",
}


# --- shared helpers ----------------------------------------------------------------------------


def _load_cfg() -> Config:
    try:
        return load_config()
    except (ValueError, TypeError, OSError) as e:
        raise Sk8sError(f"cannot load sk8s config ({config_mod.config_path()}): {e}") from e


def _namespace(explicit: str | None = None, cfg: Config | None = None) -> str:
    """The configured namespace (``sk8s config`` / SK8S_NAMESPACE), else the kube context's (if it sets one), else "sk8s"."""
    if explicit:
        return explicit
    ns = (cfg if cfg is not None else _load_cfg()).namespace
    if ns:
        return ns
    try:
        return k8s.current_namespace()
    except ConfigException as e:
        raise Sk8sError(
            f"cannot determine the namespace ({e}); set the namespace via 'sk8s config' or SK8S_NAMESPACE"
        ) from e


# --- duration parsing --------------------------------------------------------------------------

_DURATION = re.compile(r"(\d+(?:\.\d+)?)([dh]?)")


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def parse_duration_days(text: str) -> float:
    """'7d' -> 7.0, '36h' -> 1.5, '3' (bare) -> 3.0 days. Anything else is rejected."""
    m = _DURATION.fullmatch(text.strip().lower())
    if m is None:
        raise argparse.ArgumentTypeError(
            f"invalid duration {text!r}: use a number of days (7), or 7d / 36h"
        )
    value = float(m.group(1))
    return value / 24.0 if m.group(2) == "h" else value


# --- config file writer (three keys; no toml-writing dependency) -------------------------------

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def toml_string(value: str) -> str:
    """A TOML basic string: quote, backslash and control characters escaped."""
    out = []
    for ch in value:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else toml_string(key)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):  # before int: bool is an int
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return toml_string(value)
    raise Sk8sError(f"config file holds a value this writer cannot preserve: {value!r}")


def render_toml(data: dict[str, Any], comments: Sequence[str] = ()) -> str:
    """Top-level scalars, then ``comments`` (verbatim ``#`` lines), then one flat ``[table]`` per
    dict value (e.g. ``store_env``)."""
    lines = [f"{_toml_key(k)} = {_toml_scalar(v)}" for k, v in data.items() if not isinstance(v, dict)]
    lines.extend(comments)
    for k, table in data.items():
        if isinstance(table, dict):
            lines.append("")
            lines.append(f"[{_toml_key(k)}]")
            lines.extend(f"{_toml_key(tk)} = {_toml_scalar(tv)}" for tk, tv in table.items())
    return "\n".join(lines).lstrip("\n") + "\n"


def image_hint() -> str:
    """The commented line a config without a worker image carries, so the file explains itself."""
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f'# image = "YOURREG/sk8s-base:py{py}"  (build-your-own: see README)'


def write_config(path: Path, updates: dict[str, str | None]) -> None:
    """Set ``updates`` in the TOML file at ``path``, keeping any other keys already there.

    A ``None`` value removes that key (so a stale setting does not outlive the profile). When
    the result names no worker image (no ``image``, no ``image_prefix``) a commented hint line
    showing the key is written instead (see :func:`image_hint`).
    """
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as e:
            raise Sk8sError(f"cannot update {path}: {e}; fix or remove it first") from e
    for key, value in updates.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    # raises before touching the file if something is unpreservable
    text = render_toml(data, [] if data.get("image") or data.get("image_prefix") else [image_hint()])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- commands ----------------------------------------------------------------------------------


# --- GKE cloud plan (gcloud is sanctioned here, in the CLI, the way kubectl is) ---------------


@dataclass(frozen=True)
class CloudStep:
    """One gcloud command group: ``argv`` runs unless ``skip_if`` (a probe argv) exits 0."""

    title: str
    argv: tuple[str, ...]
    skip_if: tuple[str, ...] | None = None


def _run_argv(argv: Sequence[str], *, quiet: bool = False) -> int:
    """Run ``argv`` (never through a shell); the exit code. The seam tests replace."""
    out = subprocess.DEVNULL if quiet else None
    try:
        return subprocess.run(list(argv), check=False, stdout=out, stderr=out).returncode
    except FileNotFoundError:
        raise Sk8sError(f"{argv[0]} not found on PATH: install the Google Cloud CLI") from None


def gsa_email(gsa: str, project: str) -> str:
    """A GSA given as a short name ('sk8s-worker') or a full email, as an email."""
    return gsa if "@" in gsa else f"{gsa}@{project}.iam.gserviceaccount.com"


def bucket_of(store: str) -> str | None:
    """The bucket of a ``gs://bucket/prefix`` store URL, else None."""
    if not store.startswith("gs://"):
        return None
    return store[len("gs://"):].split("/", 1)[0] or None


def gke_cloud_plan(
    *, project: str, region: str, repo: str, gsa: str | None, store: str,
    namespace: str, service_account: str, bucket_role: str,
) -> tuple[list[CloudStep], list[str]]:
    """(steps, notes): the gcloud command groups that make Workload Identity and the store work.

    Without a GSA there is no identity to wire up, so only notes are produced. Every step is
    idempotent (the GSA create is skipped when it already exists; IAM bindings are additive).
    """
    steps: list[CloudStep] = []
    notes: list[str] = []
    if gsa:
        email = gsa_email(gsa, project)
        name = email.split("@", 1)[0]
        steps.append(CloudStep(
            f"create Google service account {email}",
            ("gcloud", "iam", "service-accounts", "create", name,
             "--project", project, "--display-name", "sk8s worker"),
            skip_if=("gcloud", "iam", "service-accounts", "describe", email, "--project", project),
        ))
        bucket = bucket_of(store)
        if bucket is not None:
            steps.append(CloudStep(
                f"grant {bucket_role} on gs://{bucket} to {email}",
                ("gcloud", "storage", "buckets", "add-iam-policy-binding", f"gs://{bucket}",
                 "--member", f"serviceAccount:{email}", "--role", bucket_role,
                 "--project", project),
            ))
        else:
            notes.append(f"store {store!r} is not gs://: no bucket binding is planned")
        steps.append(CloudStep(
            f"let Kubernetes ServiceAccount {namespace}/{service_account} act as {email}",
            ("gcloud", "iam", "service-accounts", "add-iam-policy-binding", email,
             "--role", "roles/iam.workloadIdentityUser",
             "--member", f"serviceAccount:{project}.svc.id.goog[{namespace}/{service_account}]",
             "--project", project),
        ))
    else:
        notes.append("no --gsa given: no Google service account or Workload Identity is planned")
    notes.append(
        f"Artifact Registry repo {region}-docker.pkg.dev/{project}/{repo}/: nodes need "
        "roles/artifactregistry.reader (the default node service account has it for same-project "
        "repos; otherwise run: gcloud artifacts repositories add-iam-policy-binding "
        f"{repo} --location {region} --project {project} "
        "--member serviceAccount:<NODE_SERVICE_ACCOUNT> --role roles/artifactregistry.reader). "
        "Not run automatically."
    )
    return steps, notes


def _print_cloud_plan(steps: Sequence[CloudStep], notes: Sequence[str]) -> None:
    for step in steps:
        print(f"# {step.title}")
        print(shlex.join(step.argv))
    for note in notes:
        print(f"# note: {note}")
    if steps:
        print(
            "# note: IAM is eventually consistent: if the Workload Identity binding reports the "
            "GSA doesn't exist yet, wait ~30s and re-run (steps are idempotent)"
        )


def _apply_cloud_plan(steps: Sequence[CloudStep]) -> None:
    for step in steps:
        if step.skip_if is not None and _run_argv(step.skip_if, quiet=True) == 0:
            print(f"cloud: {step.title}: already done")
            continue
        print(f"cloud: {step.title}", flush=True)
        rc = _run_argv(step.argv)
        if rc != 0:
            raise Sk8sError(
                f"gcloud step failed (exit {rc}): {shlex.join(step.argv)}; "
                "fix the error above and re-run (every step is safe to repeat)"
            )


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = _load_cfg()
    # Unlike the other commands, config never infers the namespace from the kube context: it
    # creates one, so the default is the dedicated "sk8s" namespace unless told otherwise.
    namespace = args.namespace or cfg.namespace or config_mod.DEFAULT_NAMESPACE
    store = args.store or cfg.store
    provider = args.provider or profile_mod.detect_provider()
    gke_given = any((args.project, args.region, args.gsa, args.bucket_role)) or args.repo != "sk8s"
    if provider != "gke" and (gke_given or args.apply_cloud):
        raise Sk8sError(
            "--project/--region/--repo/--gsa/--bucket-role/--apply-cloud are GKE options; "
            f"provider is {provider!r} (use --provider gke)"
        )
    project, region = args.project, args.region
    if provider == "gke":
        # Explicit flags win; otherwise read them off a gke_<project>_<location>_<name> context.
        parts = profile_mod.gke_context_parts(profile_mod.active_context() or "")
        if parts is not None:
            project = project or parts[0]
            region = region or profile_mod.gke_region(parts[1])
        if not (project and region):
            raise Sk8sError(
                "provider 'gke' needs --project and --region: the kube context is not named "
                "gke_<project>_<location>_<name>, so they cannot be inferred "
                "(or use --provider generic to skip the GKE adapter)"
            )
        if args.apply_cloud and not args.gsa:
            raise Sk8sError("--apply-cloud needs --gsa (the Google service account to create)")
        # The adapter derives the image prefix; a configured one is not carried over.
        extra: dict[str, Any] = dict(
            project=project, region=region, repo=args.repo, gsa=args.gsa
            and gsa_email(args.gsa, project),
            # Never derived here: the adapter owns the class, and refuses an explicit one.
            rwx_storage_class=args.rwx_storage_class, rwx_provisioner=args.rwx_provisioner,
        )
    else:
        # A generic profile keeps the class a previous run persisted; local takes none (and says
        # so if one is asked for).
        carried = cfg.rwx_storage_class if provider == "generic" else None
        extra = {
            "image_prefix": cfg.image_prefix,
            "rwx_storage_class": args.rwx_storage_class or carried,
            "rwx_provisioner": args.rwx_provisioner,
        }
    try:
        prof = profile_mod.profile_for(provider, namespace=namespace, store=store, **extra)
    except ValueError as e:
        raise Sk8sError(str(e)) from e
    steps: list[CloudStep] = []
    if provider == "gke":
        steps, notes = gke_cloud_plan(
            project=project, region=region, repo=args.repo, gsa=args.gsa,
            store=store, namespace=namespace, service_account=prof.service_account,
            bucket_role=args.bucket_role or DEFAULT_BUCKET_ROLE,
        )
    if args.dry_run:
        print("\n".join(profile_mod.plan(prof)))
        if provider == "gke":
            print("\ncloud plan (not run):")
            _print_cloud_plan(steps, notes)
        return 0
    if provider == "gke":
        if args.apply_cloud:
            _apply_cloud_plan(steps)
            for note in notes:
                print(f"note: {note}")
        else:
            print("cloud plan (not run; re-run with --apply-cloud to execute):")
            _print_cloud_plan(steps, notes)
    print("\n".join(profile_mod.apply(prof)))
    path = config_mod.config_path()
    image = args.image or os.environ.get("SK8S_IMAGE")
    write_config(
        path,
        {
            "namespace": prof.namespace,
            "store": prof.store,
            "image_prefix": prof.image_prefix,
            "rwx_storage_class": prof.rwx_storage_class,  # None (local) drops a stale key
            # Only when known: a None here would DELETE an image the file already holds.
            **({"image": image} if image else {}),
        },
    )
    print(f"wrote {path}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load_cfg()
    provider = profile_mod.detect_provider()
    namespace = _namespace(None, cfg)
    parts = profile_mod.gke_context_parts(profile_mod.active_context() or "")
    if provider == "gke" and parts is not None:
        # Project and region come from the context name; the GSA is not needed to check health.
        prof = profile_mod.profile_for(
            "gke", namespace=namespace, store=cfg.store,
            project=parts[0], region=profile_mod.gke_region(parts[1]),
        )
        if cfg.image_prefix:  # what `sk8s config` persisted (e.g. a custom --repo) wins
            prof = dataclasses.replace(prof, image_prefix=cfg.image_prefix)
    else:
        doctor_provider = "generic" if provider == "gke" else provider
        prof = profile_mod.profile_for(
            doctor_provider,
            namespace=namespace, store=cfg.store, image_prefix=cfg.image_prefix,
            # What `sk8s config` persisted, so the storage-class check covers it; local has none.
            rwx_storage_class=cfg.rwx_storage_class if doctor_provider == "generic" else None,
        )
    # Build the store here so a configured endpoint (MinIO etc.) is honoured; if it cannot be built
    # (or no client can be), leave it to doctor, which reports that as a failed check.
    store = None
    try:
        store = get_store(
            cfg.store,
            namespace=prof.namespace,
            core_api=k8s.core_api(),
            endpoint=cfg.store_endpoint,
        )
    except Exception:  # noqa: BLE001
        pass
    checks = doctor_mod.doctor(prof, store=store, store_endpoint=cfg.store_endpoint)
    if args.json:
        _print_json([{"name": c.name, "ok": c.ok, "fix_hint": c.fix_hint} for c in checks])
        return 0 if all(c.ok for c in checks) else 1
    width = max((len(c.name) for c in checks), default=0)
    for c in checks:
        print(f"{'PASS' if c.ok else 'FAIL'}  {c.name:<{width}}".rstrip())
        if not c.ok and c.fix_hint:
            print(f"      {c.fix_hint}")
    return 0 if all(c.ok for c in checks) else 1


def _cmd_image_build(args: argparse.Namespace) -> int:
    try:
        extra = {} if args.base is None else {"base": args.base}
        tag = images.build(
            args.tag, pip=args.pip, conda=args.conda, channels=args.channel, push=args.push,
            **extra,
        )
    except ValueError as e:
        raise Sk8sError(str(e)) from e
    print(f"built {tag}")
    return 0


def _cmd_image_push(args: argparse.Namespace) -> int:
    images.push(args.tag)
    print(f"pushed {args.tag}")
    return 0


_format_age = explaining.format_age


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2))


def _anchor_age_seconds(anchor: Any, now: datetime.datetime) -> float | None:
    try:
        created = datetime.datetime.fromisoformat(anchor.metadata.annotations[anchors.CREATED_AT])
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.timezone.utc)
        return (now - created).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def _anchor_age(anchor: Any, now: datetime.datetime) -> str:
    seconds = _anchor_age_seconds(anchor, now)
    return "?" if seconds is None else _format_age(seconds)


def _driver_phase(batch: Any, namespace: str, driver: str) -> str:
    if not driver:
        return "unknown"
    try:
        return job_phase(batch.read_namespaced_job(name=driver, namespace=namespace))
    except ApiException:  # 404: reaped by the TTL controller; anything else: can't tell
        return "unknown"


def _oldest_first(items: list[Any]) -> list[Any]:
    return sorted(items, key=lambda c: (c.metadata.annotations or {}).get(anchors.CREATED_AT, ""))


def _workflow_records(items: list[Any], batch: Any, namespace: str) -> list[dict[str, Any]]:
    now = datetime.datetime.now(datetime.timezone.utc)
    records = []
    for cm in _oldest_first(items):
        ann = cm.metadata.annotations or {}
        seconds = _anchor_age_seconds(cm, now)
        driver = ann.get(anchors.DRIVER_JOB, "")
        records.append({
            "id": (cm.metadata.labels or {}).get(anchors.WORKFLOW_LABEL, cm.metadata.name),
            "detached": ann.get(anchors.DETACHED) == "true",
            "created_at": ann.get(anchors.CREATED_AT),
            "age_seconds": seconds,
            "age": "?" if seconds is None else _format_age(seconds),
            "driver_job": driver,
            "driver": _driver_phase(batch, namespace, driver),
        })
    return records


def _cmd_workflows(args: argparse.Namespace) -> int:
    namespace = _namespace()
    core: Any = k8s.core_api()
    batch: Any = k8s.batch_api()
    items = core.list_namespaced_config_map(
        namespace=namespace, label_selector=f"{anchors.ANCHOR_KIND_LABEL}={anchors.ANCHOR_KIND}"
    ).items
    if args.json:
        _print_json(_workflow_records(items, batch, namespace))
        return 0
    if not items:
        print(f"no workflows in namespace {namespace}")
        return 0
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = [("ID", "DETACHED", "AGE", "DRIVER")]
    for cm in _oldest_first(items):
        ann = cm.metadata.annotations or {}
        rows.append((
            (cm.metadata.labels or {}).get(anchors.WORKFLOW_LABEL, cm.metadata.name),
            "yes" if ann.get(anchors.DETACHED) == "true" else "no",
            _anchor_age(cm, now),
            _driver_phase(batch, namespace, ann.get(anchors.DRIVER_JOB, "")),
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    for r in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip())
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    namespace = _namespace()
    core: Any = k8s.core_api()
    anchor = anchors.get_anchor(core, namespace=namespace, workflow_id=args.workflow)
    if anchor is None:
        raise Sk8sError(f"unknown workflow {args.workflow}: no anchor in namespace {namespace}")
    driver = (anchor.metadata.annotations or {}).get(anchors.DRIVER_JOB, "")
    if not driver:
        raise Sk8sError(
            f"workflow {args.workflow} has no driver job recorded "
            "(only detached workflows record one, and only after it is submitted)"
        )
    pods = core.list_namespaced_pod(namespace=namespace, label_selector=f"job-name={driver}")
    if not pods.items:
        raise Sk8sError(f"no pods found for driver job {driver} (already reaped?)")
    chunks = [
        core.read_namespaced_pod_log(name=pod.metadata.name, namespace=namespace)
        for pod in pods.items
    ]
    text = "\n".join(chunks)
    sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")
    if not text.strip():  # on stderr: stdout stays exactly the log, for pipes
        print(f"note: {explaining.EMPTY_DRIVER_LOG_NOTE}", file=sys.stderr)
    return 0


def _cmd_kill(args: argparse.Namespace) -> int:
    workflows.attach(args.workflow).kill()
    print(f"killed {args.workflow}")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    _load_cfg()  # a broken config file is a friendly error, not a traceback
    report = explaining.explain(args.workflow)
    if args.json:
        _print_json(report)
    else:
        sys.stdout.write(explaining.render(report))
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    from sk8s import gc

    cfg = _load_cfg()
    if args.json:
        _print_json(cleaning.clean_report(
            _namespace(cfg=cfg), older_than_days=args.older_than, extra_stores=[cfg.store],
        ).as_dict())
        return 0
    report = gc.SweepReport()
    deleted = cleaning.clean(
        _namespace(cfg=cfg), older_than_days=args.older_than, report=report,
        extra_stores=[cfg.store],
    )
    if deleted:
        print("\n".join(f"deleted {wf}" for wf in deleted))
    if report.deleted or report.aborted or report.memo_expired:
        print(report.summary())
    if not deleted and not (report.deleted or report.aborted or report.memo_expired):
        print("nothing to clean")
    return 0


def _shell_job(name: str, namespace: str, image: str, cfg: Config) -> client.V1Job:
    labels = {"app.kubernetes.io/managed-by": spec.MANAGED_BY, "sk8s/kind": "shell"}
    return client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=labels),
        spec=client.V1JobSpec(
            backoff_limit=0,
            ttl_seconds_after_finished=cfg.ttl_seconds_after_finished,
            active_deadline_seconds=SHELL_DEADLINE_SECONDS,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    service_account_name=cfg.service_account,
                    containers=[
                        client.V1Container(
                            name="shell",
                            image=image,
                            image_pull_policy=cfg.pull_policy,
                            command=["sh", "-c", _SHELL_HANG],
                        )
                    ],
                ),
            ),
        ),
    )


# Seams for tests (and the one place that touches the process/PATH).
_sleep = time.sleep
_clock = time.monotonic
_which = shutil.which
_execvp = os.execvp


def _wait_for_pod(
    core: Any, namespace: str, job: str, *, timeout: float,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> str:
    """Name of the job's pod once it is Running; Sk8sError if it cannot get there."""
    sleep = sleep or _sleep
    clock = clock or _clock
    deadline = clock() + timeout
    while True:
        pods = core.list_namespaced_pod(namespace=namespace, label_selector=f"job-name={job}")
        for pod in pods.items:
            status = pod.status
            phase = status.phase if status is not None else None
            if phase == "Running":
                return str(pod.metadata.name)
            if phase in ("Failed", "Succeeded"):
                raise Sk8sError(f"shell pod {pod.metadata.name} ended early ({phase})")
            for cs in (status.container_statuses or []) if status is not None else []:
                waiting = cs.state.waiting if cs.state is not None else None
                if waiting is not None and waiting.reason in _POD_FATAL_WAITING:
                    raise Sk8sError(
                        f"shell pod {pod.metadata.name} cannot start: {waiting.reason}"
                        + (f" ({waiting.message})" if waiting.message else "")
                    )
        if clock() >= deadline:
            raise Sk8sError(f"shell pod did not become Running within {timeout:.0f}s")
        sleep(1.0)


def _cmd_shell(args: argparse.Namespace) -> int:
    if _which("kubectl") is None:
        raise Sk8sError(
            "kubectl not found on PATH: 'sk8s shell' needs it for the interactive exec "
            "(install kubectl, or run the job's pod exec some other way)"
        )
    cfg = _load_cfg()
    namespace = _namespace(None, cfg)
    image = resolve_image(cfg, args.image)
    batch: Any = k8s.batch_api()
    core: Any = k8s.core_api()
    name = spec.job_name("sk8s-shell")
    batch.create_namespaced_job(namespace=namespace, body=_shell_job(name, namespace, image, cfg))
    print(f"starting shell pod (job {name}) in namespace {namespace}...", flush=True)
    try:
        pod = _wait_for_pod(core, namespace, name, timeout=SHELL_READY_TIMEOUT)
        print(
            f"job {name} is removed after {SHELL_DEADLINE_SECONDS // 3600}h; "
            f"delete earlier with: kubectl delete job {name} -n {namespace}",
            flush=True,
        )
        sys.stdout.flush()
        sys.stderr.flush()
        # The ruled exception: interactive exec needs a TTY, so hand the process to kubectl.
        # The job is not deleted afterwards (we no longer exist); its deadline + TTL reap it.
        _execvp("kubectl", ["kubectl", "exec", "-it", "-n", namespace, pod, "--", "sh"])
    except BaseException:
        try:
            batch.delete_namespaced_job(
                name=name, namespace=namespace, propagation_policy="Background"
            )
        except Exception:  # noqa: BLE001 — best effort; the original failure is what matters
            pass
        raise
    return 0  # only reached when execvp is replaced (tests)


def _cmd_data_describe(args: argparse.Namespace) -> int:
    result = datadescribe.describe(args.target, samples=args.samples, max_objects=args.max_objects)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(datadescribe.format_description(result))
    return 0


# --- parser ------------------------------------------------------------------------------------


def _add_json(p: argparse.ArgumentParser, shape: str) -> None:
    p.add_argument("--json", action="store_true", help=f"print machine-readable JSON instead: {shape}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sk8s", description="Run Python functions on Kubernetes.")
    parser.add_argument("--version", action="version", version=f"sk8s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p = sub.add_parser("config", help="prepare a namespace for sk8s and write the client config")
    p.add_argument("--namespace", help="namespace to create and configure (default: sk8s, or the namespace already configured)")
    p.add_argument("--store", help="blob store (default: configured, else 'k8s')")
    p.add_argument("--image", help="worker image to write as `image` in the config file "
                   "(default: $SK8S_IMAGE if set, else the existing value, else a commented hint)")
    p.add_argument("--provider", help="local | generic | gke (default: detected from the kube context)")
    p.add_argument("--project", help="gke: Google Cloud project id (required, not inferred)")
    p.add_argument("--region", help="gke: region of the Artifact Registry repo, e.g. us-central1 (required)")
    p.add_argument("--repo", default="sk8s", help="gke: Artifact Registry repo name (default: sk8s)")
    p.add_argument("--gsa", help="gke: Google service account (name or email) workers act as via Workload Identity")
    p.add_argument("--bucket-role", help=f"gke: role granted to the GSA on the store bucket (default: {DEFAULT_BUCKET_ROLE})")
    p.add_argument("--rwx-storage-class", help="generic: StorageClass for ReadWriteMany volumes (persisted; gke derives standard-rwx itself)")
    p.add_argument("--rwx-provisioner", help="generic: with --rwx-storage-class, CREATE that StorageClass with this provisioner if the cluster lacks it")
    p.add_argument("--apply-cloud", action="store_true", help="gke: also RUN the printed gcloud commands (default: print only)")
    p.add_argument("--dry-run", action="store_true", help="print the plan; apply nothing")
    p.set_defaults(func=_cmd_config)

    p = sub.add_parser("doctor", help="check that the cluster is ready for sk8s")
    _add_json(p, "a list of {name, ok, fix_hint}, one per check")
    p.set_defaults(func=_cmd_doctor)

    img = sub.add_parser("image", help="build or push worker images")
    img_sub = img.add_subparsers(dest="image_command", metavar="ACTION", required=True)
    p = img_sub.add_parser("build", help="build a worker image with extra dependencies")
    p.add_argument("--tag", required=True, help="image tag to build")
    p.add_argument("--pip", action="append", default=[], metavar="PKG", help="pip dep (repeatable)")
    p.add_argument("--conda", action="append", default=[], metavar="PKG", help="conda dep (repeatable)")
    p.add_argument("--channel", action="append", default=[], metavar="CH", help="conda channel (repeatable)")
    p.add_argument("--base", metavar="IMG", help="base image to layer pip deps on "
                   "(default: the official worker image; not valid with --conda)")
    p.add_argument("--push", action="store_true", help="docker push after building")
    p.set_defaults(func=_cmd_image_build)
    p = img_sub.add_parser("push", help="docker push an image")
    p.add_argument("--tag", required=True, help="image tag to push")
    p.set_defaults(func=_cmd_image_push)

    p = sub.add_parser("workflows", help="list workflows in the namespace")
    _add_json(p, "a list of {id, detached, created_at, age, age_seconds, driver_job, driver}")
    p.set_defaults(func=_cmd_workflows)

    p = sub.add_parser(
        "explain", help="everything about one workflow in one report: driver, jobs, what is blocking"
    )
    p.add_argument("workflow")
    _add_json(p, "the full report (see sk8s.explain); exit 0 even when unhealthy, 1 if unknown")
    p.set_defaults(func=_cmd_explain)

    p = sub.add_parser("logs", help="print a workflow's driver logs")
    p.add_argument("workflow")
    p.set_defaults(func=_cmd_logs)

    p = sub.add_parser("kill", help="delete a workflow and everything it owns")
    p.add_argument("workflow")
    p.set_defaults(func=_cmd_kill)

    p = sub.add_parser("clean", help="delete expired workflows")
    p.add_argument(
        "--older-than", type=parse_duration_days, metavar="D",
        help="age limit instead of each workflow's retention: 7d, 36h, or bare days (7)",
    )
    _add_json(p, '{"deleted": [ids], "kept": [{workflow, reason}]}')
    p.set_defaults(func=_cmd_clean)

    p = sub.add_parser("shell", help="open a shell in a pod in the cluster (needs kubectl)")
    p.add_argument("--image", help="image for the shell pod (default: the worker image)")
    p.set_defaults(func=_cmd_shell)

    data = sub.add_parser("data", help="inspect data (formats, sizes, cohorts)")
    data_sub = data.add_subparsers(dest="data_command", metavar="ACTION", required=True)
    p = data_sub.add_parser(
        "describe", help="sniff the format of a file, or summarize a directory / gs:// / s3:// prefix",
    )
    p.add_argument("target", metavar="PATH_OR_URI", help="local file or directory, or a gs:// / s3:// object or prefix")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.add_argument("--samples", type=_positive_int, default=datadescribe.SAMPLES, metavar="N",
                   help=f"cohort: files to sniff and report in detail (default: {datadescribe.SAMPLES})")
    p.add_argument("--max-objects", type=_positive_int, default=datadescribe.MAX_OBJECTS, metavar="N",
                   help=f"cohort: listing cap (default: {datadescribe.MAX_OBJECTS})")
    p.set_defaults(func=_cmd_data_describe)
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Cross-flag usage errors, reported the argparse way (usage + exit 2)."""
    if getattr(args, "image_command", None) != "build":
        return
    if not args.pip and not args.conda:
        parser.error("image build: give at least one --pip or --conda dependency")
    if args.channel and not args.conda:
        parser.error("image build: --channel requires --conda")


def run(argv: Sequence[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate(parser, args)
    try:
        return int(args.func(args))
    except Sk8sError as e:
        print(f"sk8s: error: {e}", file=sys.stderr)
    except ApiException as e:
        print(f"sk8s: error: Kubernetes API error: HTTP {e.status} {e.reason}", file=sys.stderr)
    except ConfigException as e:
        print(f"sk8s: error: no usable kube config ({e})", file=sys.stderr)
    except HTTPError as e:
        print(f"sk8s: error: cannot reach the cluster ({type(e).__name__})", file=sys.stderr)
    except OSError as e:
        print(f"sk8s: error: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        print("sk8s: interrupted", file=sys.stderr)
        return 130
    return 1


def main() -> int:
    return run(sys.argv[1:])
