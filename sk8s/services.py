
import subprocess
import re
import jinja2

import sk8s

#### Launch k8s Services driven by python lambdas

service_template = """apiVersion: apps/v1
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

          func = sk8s.deserialize_func("{{code}}")

          config = {{config}}
          sk8s.configs.save_config(config)

          try:
              json.dump(func(), sys.stdout)
          except Exception as e:
              raise e

        {%- if (requests is defined and requests|length > 0) or (limits is defined and limits|length > 0) %}
        resources:
        {%- endif %}
        {%- if requests is defined and requests|length > 0 %}
            requests:
        {%- endif %}
        {%- for key, value in requests.items() %}
                {{key}}: {{value}}
        {%- endfor %}
        {%- if limits is defined and limits|length > 0 %}
            limits:
        {%- endif %}
        {%- for key, value in limits.items() %}
                {{key}}: {{value}}
        {%- endfor %}
        {%- if volumes is defined and volumes|length > 0 %}
        volumeMounts:
        {%- for volname, mountpath in volumes.items() %}
        - mountPath: {{mountpath}}
          name: {{volname}}
        {%- endfor %}
        {%- endif %}
        ports:
        - containerPort: {{port}}
    
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
  - port: {{port}}
    targetPort: {{port}}
  selector:
    app.kubernetes.io/name: {{name}}
    app.kubernetes.io/component: backend
"""


def service(func, *args,
        image=None,
        volumes={},
        requests=dict(),
        limits=dict(),
        port=5000,
        service_template=service_template,
        imagePullPolicy=None,
        backoffLimit=0,
        name="service-{s}",
        test=False,
        dryrun=False,
        state=None,
        config=None,
        export_config=True,
        debug=False):
    # Should do it this way, but having problems. Reverting for now:
    # job_template = importlib.resources.read_text("sk8s", "job_template.yaml")

    if config is None:
        config = sk8s.configs.load_config()

    if image is None:
        image = config["docker_image_prefix"] + "jobs"

    if imagePullPolicy is None:
        imagePullPolicy = config["docker_default_pull_policy"]

    if state is None:
        code = sk8s.util.serialize_func(lambda a=args: func(*a))
    else:
        code = sk8s.util.serialize_func(state.memoize(lambda a=args: func(*a)))

    if volumes.__class__ == str:
        volumes = {volumes: f"/mnt/{volumes}"}
    if volumes.__class__ == list:
        volumes = {name: f"/mnt/{name}" for name in volumes}

    t = jinja2.Template(service_template)
    s = sk8s.util.random_string(5)
    job = name=name.format(s=s)
    j = t.render(name=job,
                 code=code,
                 image=image,
                 requests=requests,
                 limits=limits,
                 volumes=volumes,
                 port=port,
                 config=config if export_config else sk8s.configs.default_config,
                 imagePullPolicy=imagePullPolicy,
                 backoffLimit=backoffLimit)

    if dryrun:
        return j

    proc = subprocess.run("kubectl apply -f -",
                          shell=True,
                          input=j.encode("utf-8"),
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL,
                          check=True)

    namespace = subprocess.run("kubectl config view --minify -o jsonpath='{..namespace}'",
                               check=True, shell=True, stdout=subprocess.PIPE,
                               encoding="utf-8").stdout.strip()
    namespace = re.sub("'", "", namespace)

    url = f"http://{name}.{namespace}.svc.cluster.local:{port}"

    return url


def stop_service(url):
    name, namespace, port = re.findall(f"http://(.+?)\.(.*?)\.svc\.cluster\.local:(\d+)",
                                       url)[0]
    subprocess.run(f"kubectl delete service {name}", check=True, shell=True,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8")
    subprocess.run(f"kubectl delete deployment {name}", check=True, shell=True,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8")