import jinja2
import subprocess
import k8s.util as util


default_volume_template = """apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{name}}
spec:
  accessModes: {{accessModes}}
  resources:
   requests:
    storage: {{size}}
"""

#  storageClassName: manual


def create_volume(size, accessModes=["ReadWriteMany"], name=None, template=default_volume_template, **kwargs):
    template = jinja2.Template(default_volume_template)

    if name is None:
        string = util.random_string(5)
        name = f"volume-{string}"

    template_args = kwargs.copy()
    template_args["accessModes"] = accessModes
    template_args["size"] = size
    template_args["name"] = name

    volume_yaml = template.render(**template_args)

    subprocess.run("kubectl apply -f -",
                   shell=True,
                   input=volume_yaml.encode("utf-8"),
                   stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE,
                   check=True)

    return name
