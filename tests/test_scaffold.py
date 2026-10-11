import importlib.metadata

import sk8s


def test_version() -> None:
    assert sk8s.__version__ == "2.0.0a0"


def test_installed_metadata_version_matches_package() -> None:
    # Guards pyproject.toml vs sk8s/__init__.py drift (the release guard doesn't check this).
    assert importlib.metadata.version("sk8s") == sk8s.__version__


def test_public_exports() -> None:
    for name in (
        "LocalCluster", "Sk8sError", "StoreError", "BlobNotFound", "BlobTooLarge",
        "VersionMismatch", "JobTimeout", "PodFailed", "ImagePullError",
        "RemoteException", "RemoteTraceback", "MapFailed",
    ):
        assert name in sk8s.__all__
        assert getattr(sk8s, name).__name__ == name
