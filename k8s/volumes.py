import jinja2
import subprocess
import k8s.util as util
import k8s.configs as configs


# NOTE: To get ReadWriteMany volumes on these providers, you need to do some cluster config:
# GKE: https://cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/filestore-csi-driver


default_volume_template = """apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{name}}
spec:
  accessModes: {{accessModes}}
  {%- if storageClassName is defined %}
  storageClassName: {{storageClassName}}
  {%- endif %}
  resources:
   requests:
    storage: {{size}}
"""


def create_volume(size, name=None, accessModes=["ReadWriteMany"], template=default_volume_template, dryrun=False, **kwargs):
    template = jinja2.Template(default_volume_template)

    if name is None:
        string = util.random_string(5)
        name = f"volume-{string}"

    template_args = kwargs.copy()
    template_args["accessModes"] = accessModes
    template_args["size"] = size
    template_args["name"] = name

    if "ReadWriteMany" in template_args["accessModes"]:
        config = configs.load_config()
        template_args["storageClassName"] = config["default_readwritemany_storageclass"]

    volume_yaml = template.render(**template_args)

    if dryrun:
      return volume_yaml

    subprocess.run("kubectl apply -f -",
                   shell=True,
                   input=volume_yaml.encode("utf-8"),
                   check=True)

    return name


def delete_volume(volume):
    subprocess.run(f"kubectl delete pvc {volume}",
                   shell=True,
                   check=True)