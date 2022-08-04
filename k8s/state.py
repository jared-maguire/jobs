
import jinja2
import subprocess
import k8s.util
import k8s.volumes


default_mongodb_template = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{name}}
  labels:
    app.kubernetes.io/name: {{name}}
    app.kubernetes.io/component: backend
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: {{name}}
      app.kubernetes.io/component: backend
  replicas: 1
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {{name}}
        app.kubernetes.io/component: backend
    spec:
      containers:
      - name: {{name}}
        image: mongo:4.2
        args:
          - --bind_ip
          - 0.0.0.0
        resources:
          requests:
            cpu: 100m
            memory: 100Mi
        ports:
        - containerPort: 27017
---
apiVersion: v1
kind: Service
metadata:
  name: {{name}}
  labels:
    app.kubernetes.io/name: {{name}}
    app.kubernetes.io/component: backend
spec:
  ports:
  - port: 27017
    targetPort: 27017
  selector:
    app.kubernetes.io/name: {{name}}
    app.kubernetes.io/component: backend
"""


def create_mongo_db(name=None, namespace=None, dryrun=False, template=default_mongodb_template, **kwargs):
    template = jinja2.Template(template)

    if name is None:
        string = k8s.util.random_string(5)
        name = f"mongodb-{string}"

    if namespace is None:
        namespace = k8s.util.get_current_namespace()

    template_args = kwargs.copy()
    template_args["name"] = name

    db_yaml = template.render(**template_args)

    if dryrun:
      return db_yaml

    subprocess.run(f"kubectl apply -f - --namespace {namespace}",
                   shell=True,
                   input=db_yaml.encode("utf-8"),
                   check=True)

    url = f"mongodb://{name}.{namespace}"

    return dict(name=name, url=url)


def delete_mongo_db(name):
    subprocess.run(f"kubectl delete service {name} && kubectl delete deployment {name}",
                   shell=True, check=True)


class WorkflowState:
    def __init__(self, name=None):
        if name is None:
            # Generate a name.
            self.name = "wfs-" + k8s.util.random_string(5)

            # Create a small rwx volume named wfs-{name}.
            k8s.volumes.create_volume("100Mi", name=self.name, accessModes=["ReadWriteMany"])
        else:
            self.connect(name)

    def set(key, val):
        self.data[key] = val
        return self.data[key]

    def get(key):
        return self.data[key]

    def connect(self, name):
        self.name = name