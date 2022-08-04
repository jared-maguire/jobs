
from re import A
import jinja2
import subprocess
import pymongo
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


def delete_mongo_db(db):
    if db.__class__ == dict:
      name = db["name"]
    else:
      name = db
    subprocess.run(f"kubectl delete service {name} && kubectl delete deployment {name}",
                   shell=True, check=True)


def mongo_db_port_forward(db):
    name = db["name"]
    cmd = f"kubectl port-forward service/{name} 27017:27017"
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


# Note: this class only works within pods right now.
class WorkflowState:
    def __init__(self, db=None):
        if db is None:
          self.db = create_mongo_db()
        else:
          self.db = db
        self.name = db["url"]

    def set(self, key, val):
        client = pymongo.client(self.db["url"])
        client.state.state.update(dict(key=key), dict(val=val), dict(upsert=True))
        return None

    def get(self, key):
        client = pymongo.client(self.db["url"])
        result = client.state.state.find_one(dict(key=key))
        return result["val"]

    def __setitem__(self, key, val):
        return self.set(key, val)

    def __getitem__(self, key):
        return self.get(key)

    def __delitem__(self, key):
        raise NotImplementedError