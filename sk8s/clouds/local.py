### GKE-specific configuration code.

import jinja2
import subprocess
import sk8s.configs


# Overall Local cluster config:

def config_cluster():
    sk8s.configs.save_config(sk8s.default_config)
    return sk8s.default_config