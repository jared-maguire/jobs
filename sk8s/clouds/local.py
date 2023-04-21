### GKE-specific configuration code.

import jinja2
import subprocess
import sk8s.configs



# Enable ReadWriteMany volumes via Google FileStore:

#def config_storageclass_defaults():
#    config = sk8s.configs.load_config()
#    config["default_readwritemany_storageclass"] = "standard-rwx"
#    sk8s.configs.save_config(config)


# Overall Local cluster config:

def config_cluster(project, dryrun=False):
    sk8s.configs.save_config(sk8s.default_config)
    return sk8s.default_config