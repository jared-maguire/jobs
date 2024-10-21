### GKE-specific configuration code.

import subprocess
import sk8s.configs
import importlib
import jinja2


filestore_storageclass="""apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: standard-rwx
provisioner: filestore.csi.storage.gke.io
volumeBindingMode: Immediate
allowVolumeExpansion: true
parameters:
  tier: standard
  network: default"""


# If needed, add a storage class to talk to FileStore. (autopilot clusters come with this by default)
def add_rwx_storage_class():
    subprocess.run("kubectl apply -f -",
                   shell=True,
                   input=filestore_storageclass.encode("utf-8"),
                   stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL,
                   check=True)


# Enable ReadWriteMany volumes via Google FileStore:
def config_storageclass_defaults():
    config = sk8s.configs.load_config()
    config["default_readwritemany_storageclass"] = "standard-readwritemany"
    sk8s.configs.save_config(config)


# Overall GKE cluster config:

def config_cluster(project, namespace):
    # Create service account with permissions to apply changes to the cluster 
    config = importlib.resources.read_text("sk8s", "cluster_config.yaml")
    config = jinja2.Template(config).render(namespace=namespace)
    subprocess.run("kubectl apply -f -", input=config.encode("utf-8"), check=True, shell=True) 

    config_storageclass_defaults()

    # TODO: get current google project

    google_config = dict(
                      cluster_type="gke",
                      docker_image_prefix=f"gcr.io/{project}/",
                      docker_default_pull_policy="Always",
                      docker_build_default_push_policy=True,
                      ecr_create_repo_on_push=False,
                      default_storageclass="standard",
                      service_account_name="sk8s",
                     )
    config = sk8s.configs.load_config()
    config.update(google_config)
    sk8s.configs.save_config(config)

    return config