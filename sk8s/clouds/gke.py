### GKE-specific configuration code.

import jinja2
import subprocess
import sk8s.configs



# Enable ReadWriteMany volumes via Google FileStore:

def config_storageclass_defaults():
    config = sk8s.configs.load_config()
    config["default_readwritemany_storageclass"] = "standard-rwx"
    sk8s.configs.save_config(config)


# Overall GKE cluster config:

def config_cluster(project, dryrun=False):
    config_storageclass_defaults()

    # TODO: get current google project

    google_config = dict(
                      docker_image_prefix=f"gcr.io/{project}/",
                      docker_default_pull_policy="Always",
                      docker_build_default_push_policy=True,
                      default_storageclass="standard",
                     )
    config = sk8s.configs.load_config()
    config.update(google_config)
    sk8s.configs.save_config(config)

    return config