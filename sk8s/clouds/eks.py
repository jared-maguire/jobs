### EKS-specific configuration code.

import importlib
import subprocess
import sk8s.configs
import jinja2


# Enable ReadWriteMany volumes via EFS:
def config_storageclass_defaults():
    config = sk8s.configs.load_config()
    config["default_readwritemany_storageclass"] = "efs"
    sk8s.configs.save_config(config)


# Overall EKS cluster config:

def config_cluster(account, region, namespace, results_prefix):
    # Create service account with permissions to apply changes to the cluster 
    config = importlib.resources.read_text("sk8s", "cluster_config.yaml")
    config = jinja2.Template(config).render(namespace=namespace)
    subprocess.run("kubectl apply -f -", input=config.encode("utf-8"), check=True, shell=True) 

    # Add the EFS storage class
    config_storageclass_defaults()

    # Get the current username
    username = subprocess.run("whoami", shell=True, capture_output=True, check=True, text=True).stdout.strip()

    eks_config = dict(
                      cluster_type="eks",
                      docker_image_prefix=f"{account}.dkr.ecr.{region}.amazonaws.com/{username}_",
                      docker_default_pull_policy="Always",
                      docker_build_default_push_policy=True,
                      ecr_create_repo_on_push=True,
                      default_storageclass="efs",
                      service_account_name="sk8s",
                      result_obs_prefix=results_prefix,
                     )
    sk8s_config = sk8s.configs.load_config()
    sk8s_config.update(eks_config)
    sk8s.configs.save_config(sk8s_config)

    return config
