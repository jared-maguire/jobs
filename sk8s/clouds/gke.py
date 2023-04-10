### GKE-specific configuration code.

import jinja2
import subprocess
import k8s.configs



# Enable ReadWriteMany volumes via Google FileStore:

default_gke_readwritemany_storage_class_template = """apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: {{tier}}-readwritemany
provisioner: filestore.csi.storage.gke.io
volumeBindingMode: Immediate
allowVolumeExpansion: true
parameters:
  tier: {{tier}}
  network: {{ network|default("default", true) }}"""


def config_readwritemany_storage(network="default", dryrun=False):
    template = jinja2.Template(default_gke_readwritemany_storage_class_template)

    yamls = []
    for tier in ["standard", "premium"]:
        storage_class_yaml = template.render(network=network, tier=tier)
        if dryrun:
            print(storage_class_yaml + "\n")
        else:
            subprocess.run("kubectl apply -f -",
                           shell=True,
                           input=storage_class_yaml.encode("utf-8"),
                           check=True)


def config_storageclass_defaults():
    config = k8s.configs.load_config()
    config["default_readwritemany_storageclass"] = "standard-readwritemany"
    k8s.configs.save_config(config)


# Overall GKE cluster config:

def config_cluster(dryrun=False):
    rwm_result = config_readwritemany_storage(dryrun=dryrun)
    config_storageclass_defaults()

    # TODO: get current google project

    google_config = dict(
                      docker_image_prefix=f"gcr.io/{project}/",
                      docker_default_pull_policy="Always",
                      docker_build_default_push_policy=True,
                      default_storageclass="standard",
                     )
    config = k8s.configs.load_config()
    config.update(google_config)

    return config