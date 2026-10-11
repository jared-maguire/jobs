__version__ = "2.0.0a0"

from sk8s.api import (  # noqa: E402
    configure, map, run, starmap, submit, submit_map, submit_starmap,
)
from sk8s.cluster import Cluster  # noqa: E402
from sk8s.config import Config, load_config  # noqa: E402
from sk8s.future import JobFuture  # noqa: E402

__all__ = [
    "__version__", "configure", "run", "submit", "map", "starmap",
    "submit_map", "submit_starmap", "Cluster", "Config", "load_config",
    "JobFuture",
]
