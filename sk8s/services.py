
import jinja2
import subprocess
import pymongo
import sk8s.util
import sk8s.volumes
import hashlib
import sys
from collections import namedtuple


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
              print(e)
              json.dump("!!! ERROR, service function threw exception.", sys.stdout)
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
    name: port{{port}}
  {%- endfor -%}
  {%- endif %}
  selector:
    app.kubernetes.io/name: {{name}}
    app.kubernetes.io/component: backend
"""


def service(func, *args,
            name=None,
            ports=[5000],
            timeout=30.0,
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
                   check=True,
                   capture_output=True)
    
    # wait for it to come up
    import json, time
    start_time = time.time()
    while True:

      try:
        txt = subprocess.run(f"kubectl get deployment {name} -o json",
                             check=True, shell=True, stdout=subprocess.PIPE,
                             encoding="utf-8").stdout
        d = json.loads(txt)

        if "readyReplicas" in d["status"]:
          ready_replicas = d["status"]["readyReplicas"]
          #print(f"waiting... ready_replicas={ready_replicas}", flush=True)
          if ("readyReplicas" in d["status"]) and (d["status"]["readyReplicas"] > 0):
            break

        wait_time = time.time() - start_time
        if wait_time > timeout:
          raise Exception(f"sk8s.service(): waiting for service timed out.")

        time.sleep(1)

      except subprocess.CalledProcessError as cpe_exception:
        wait_time = time.time() - start_time
        if wait_time > timeout:
          raise Exception(f"sk8s.service(): waiting for service timed out.") from cpe_exception
        continue

    #print(f"proceeding... ready_replicas={ready_replicas}", flush=True)

    return name


def shutdown_service(service):
    if service.__class__ == dict:
      name = service["name"]
    else:
      name = service
    subprocess.run(f"kubectl delete --ignore-not-found=true deployment/{name} service/{name}",
                   shell=True, check=True, capture_output=True)


def forward(service, remote_port, local_port=""):
    if sk8s.util.in_pod():
        d = dict(proc=None, url=f"http://{service}.{sk8s.util.get_current_namespace()}.svc.cluster.local:{remote_port}")
        return namedtuple('PortForward', d.keys())(**d)
    else:
        if service.__class__ == dict:
            name = service["name"]
        else:
            name = service
        cmd = f"kubectl port-forward service/{name} {local_port}:{remote_port}"
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if local_port == "":
            local_port = int(proc.stdout.readlines(1)[0].split()[2].decode("utf-8").split(":")[1])
        d = dict(proc=proc, url=f"http://localhost:{local_port}")
        return namedtuple('PortForward', d.keys())(**d)


#####################################################################
#
# And now, for fun, a handy service!

from flask import Flask, request, jsonify
import requests
from threading import Thread
import time
import json

class KeyValueStore:
    def __init__(self, host="0.0.0.0"):
        self.app = Flask(__name__)
        self.host = host
        self.store = {}
        self.app.add_url_rule('/store', view_func=self.handle_request, methods=['POST', 'GET'])
        self.app.add_url_rule('/store/all', view_func=self.handle_get_all, methods=['GET'])

    def handle_request(self):
        if request.method == 'POST':
            data = request.get_json()
            key = data['key']
            value = data['value']
            self.store[key] = value
            return jsonify({'result': f'Key: {key} and Value: {value} added to the store'})
        else:
            key = request.args.get('key')
            if key in self.store:
                return jsonify({'key': key, "value": self.store[key]})
            else:
                #return jsonify({'ERROR': f'Key {key} not found in store'})
                return jsonify(None)

    def handle_get_all(self):
        return jsonify(self.store)

    def background_task(self):
        while True:
            # Add your background task code here
            time.sleep(1)

    def run(self):
        thread = Thread(target=self.background_task)
        thread.start()
        self.app.run(host=self.host)

    @staticmethod
    def put(url, key, value):
        response = requests.post(url + "/store", json=dict(key=key, value=value))
        assert(response.ok)  # Could raise an exception in a more appropriate way...
        return response

    @staticmethod
    def get(url, key):
        response = requests.get(url + "/store", dict(key=key))
        assert(response.ok)  # Could raise an exception in a more appropriate way...
        return json.loads(response.content)

    @staticmethod
    def get_all(url):
        response = requests.get(url + "/store/all")
        assert(response.ok)  # Could raise an exception in a more appropriate way...
        return json.loads(response.content)

    @staticmethod
    def wait_until_up(service_name):
      # This checks that the Service is up:
      not_up = True
      while not_up:
          fwd = forward(service_name, 5000, 5000)
          time.sleep(1)
          try:
              KeyValueStore.get_all(fwd.url)
              not_up = False
          except Exception as e:
              fwd.proc.terminate()
              stdout, stderr = fwd.proc.communicate()
              fwd.proc.wait()
              time.sleep(1)


def kvs_service():
  def service_func():
    kvs = KeyValueStore()
    kvs.run()

  return service(service_func, ports=[5000])


class KVSClient:
    def __init__(self, service):
      self.service = service  
      self.fwd = forward(service, 5000)

    def close(self):
      if self.fwd.proc is not None:
        self.fwd.proc.terminate()
        self.fwd.proc.wait()
    
    def __del__(self):
      self.close()

    def put(self, key, value):
      return KeyValueStore.put(self.fwd.url, key, value)
    
    def get(self, key):
      return KeyValueStore.get(self.fwd.url, key)["value"]

    def delete(self, key):
      return KeyValueStore.delete(self.fwd.url, key)

    def __getitem__(self, key):
      return self.get(key)
    
    def __setitem__(self, key, value):
      return self.put(key, value)
    
    def __delitem__(self, key):
      return self.delete(key, None)
    
    def keys(self):
      return self.get_all().keys()
    
    def values(self):
      return self.get_all().values()
    
    def items(self):
      return self.get_all().items()

    def get_all(self):
      return KeyValueStore.get_all(self.fwd.url) 