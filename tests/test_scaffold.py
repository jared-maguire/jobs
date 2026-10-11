import sk8s


def test_version() -> None:
    assert sk8s.__version__ == "2.0.0a0"


def test_public_exports() -> None:
    for name in (
        "LocalCluster", "Sk8sError", "StoreError", "BlobNotFound", "BlobTooLarge",
        "VersionMismatch", "JobTimeout", "PodFailed", "ImagePullError",
        "RemoteException", "RemoteTraceback", "MapFailed",
    ):
        assert name in sk8s.__all__
        assert getattr(sk8s, name).__name__ == name
