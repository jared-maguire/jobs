
import jinja2
import subprocess
import pymongo
import sk8s.util
import sk8s.volumes
import hashlib
import sys


default_service_template = """apiVersion: apps/v1
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
        image: {{image}}
        imagePullPolicy: {{imagePullPolicy}}
        command:
        - python
        - -c
        - |
          import dill as pickle
          import base64
          import json
          import sys
          import sk8s

          config = {{config}}
          sk8s.configs.save_config(config)

          func = sk8s.deserialize_func("{{code}}")

          try:
              json.dump(func(), sys.stdout)
          except Exception as e:
              raise e

        args:
          - --bind_ip
          - 0.0.0.0
        resources:
          requests:
            cpu: 100m
            memory: 100Mi
        {%- if (ports is defined and ports|length > 0) %}
        ports:
        {%- for port in ports %}
        - containerPort: {{port}}
        {%- endfor %}
        {%- endif %}
---
apiVersion: v1
kind: Service
metadata:
  name: {{name}}
  labels:
    app.kubernetes.io/name: {{name}}
    app.kubernetes.io/component: backend
spec:
  {%- if (ports is defined and ports|length > 0) %}
  ports:
  {%- for port in ports %}
  - port: {{port}}
    targetPort: {{port}}
  {%- endfor -%}
  {%- endif %}
  selector:
    app.kubernetes.io/name: {{name}}
    app.kubernetes.io/component: backend
"""


def service(func, *args,
            name=None,
            ports=[21, 22, 80, 5000],
            namespace=None,
            dryrun=False,
            template=default_service_template,
            config=None,
            image=None,
            imagePullPolicy=None,
            export_config=True,
            **kwargs):

    template = jinja2.Template(template)

    if name is None:
        string = sk8s.util.random_string(5)
        name = f"service-{string}"

    if namespace is None:
        namespace = sk8s.util.get_current_namespace()

    if config is None:
        config = sk8s.configs.load_config()

    if image is None:
        image = config["docker_image_prefix"] + "jobs"

    if imagePullPolicy is None:
        imagePullPolicy = config["docker_default_pull_policy"]

    code = sk8s.util.serialize_func(lambda a=args: func(*a))

    template_args = kwargs.copy()
    template_args["name"] = name
    template_args["image"] = image
    template_args["ports"] = ports
    template_args["namespace"] = namespace
    template_args["imagePullPolicy"] = imagePullPolicy
    template_args["config"] = config if export_config else sk8s.configs.default_config,
    template_args["code"] = code

    service_yaml = template.render(**template_args)

    if dryrun:
      return service_yaml

    subprocess.run(f"kubectl apply -f - --namespace {namespace}",
                   shell=True,
                   input=service_yaml.encode("utf-8"),
                   check=True)
    
    # wait for it to come up
    import json, time
    while True:
      txt = subprocess.run(f"kubectl get deployment {name} -o json",
                   check=True, shell=True, stdout=subprocess.PIPE,
                   encoding="utf-8").stdout
      d = json.loads(txt)
      if "readyReplicas" in d["status"]:
        ready_replicas = d["status"]["readyReplicas"]
        print(f"waiting... ready_replicas={ready_replicas}", flush=True)
        if ("readyReplicas" in d["status"]) and (d["status"]["readyReplicas"] > 0):
          break
      time.sleep(1)

    print(f"proceeding... ready_replicas={ready_replicas}", flush=True)

    return name


def shutdown_service(service):
    if service.__class__ == dict:
      name = service["name"]
    else:
      name = service
    subprocess.run(f"kubectl delete service {name} && kubectl delete deployment {name}",
                   shell=True, check=True)


def forward(service, remote_port, local_port):
    if service.__class__ == dict:
      name = service["name"]
    else:
      name = service
    cmd = f"kubectl port-forward service/{name} {remote_port}:{local_port}"
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return dict(proc=proc, url=f"http://localhost:{local_port}")
