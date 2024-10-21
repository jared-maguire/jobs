### EKS-specific configuration code.

import importlib
import subprocess
import sk8s.configs


# Enable ReadWriteMany volumes via EFS:
def config_storageclass_defaults():
    config = sk8s.configs.load_config()
    config["default_readwritemany_storageclass"] = "efs"
    sk8s.configs.save_config(config)


# Overall EKS cluster config:

def config_cluster(account, region):
    # Create service account with permissions to apply changes to the cluster 
    config = importlib.resources.read_text("sk8s", "eks_cluster_config.yaml")
    subprocess.run("kubectl apply -f -", input=config.encode("utf-8"), check=True, shell=True) 

    # Add the EFS storage class
    config_storageclass_defaults()

    eks_config = dict(
                      cluster_type="eks",
                      docker_image_prefix=f"{account}.dkr.ecr.{region}.amazonaws.com/",
                      docker_default_pull_policy="Always",
                      docker_build_default_push_policy=True,
                      default_storageclass="standard",
                     )
    config = sk8s.configs.load_config()
    config.update(eks_config)
    sk8s.configs.save_config(config)

    return config