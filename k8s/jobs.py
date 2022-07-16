#!/usr/bin/env python

import os

import dill as pickle
pickle.settings['recurse'] = True

import time
import base64
import json
import jinja2
import subprocess

from k8s.util import *


def check_cluster_config():
    svc_acct = "internal-kubectl" in subprocess.run("kubectl get serviceaccounts",
                                                    check=True,
                                                    stdout=subprocess.PIPE).stdout.decode("utf-8")
    return svc_acct


# This should be in a file inside the package, but I'm having problems with that right now...
default_job_template = """apiVersion: batch/v1
kind: Job
metadata:
  name: {{name}}
spec:
  template:
    spec:
      serviceAccountName: internal-kubectl
      containers:
      - name: worker
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
          import k8s

          {% for module in imports %}
          import {{module}} {% endfor %}

          func = k8s.deserialize_func("{{code}}")

          json.dump(func(), sys.stdout)
        {% for volume in volumes %}
        volumeMounts:
        - mountPath: "/mnt/{{volume}}"
          name: {{volume}}
        {% endfor %}

      restartPolicy: Never
  backoffLimit: 1
"""


def run(func, *args, image="jobs", volumes=[], imports=[], job_template=default_job_template, imagePullPolicy="Never", test=False, dryrun=False, debug=False):
    # Should do it this way, but having problems. Reverting for now:
    # job_template = importlib.resources.read_text("k8s", "job_template.yaml")

    code = serialize_func(lambda a=args: func(*a))

    if test:
        func_2 = pickle.loads(base64.b64decode(code))
        return func_2()

    t = jinja2.Template(job_template)
    s = random_string(5)
    j = t.render(name=f"job-{s}", code=code, image=image, volumes=volumes, imports=imports, imagePullPolicy=imagePullPolicy)

    if dryrun:
        return j

    subprocess.run("kubectl apply -f -",
                   shell=True,
                   input=j.encode("utf-8"),
                   stdout=subprocess.PIPE,
                   check=True)
                   #stderr=subprocess.PIPE,

    return f"job-{s}"


def wait(job_name, timeout=None, verbose=False, delete=True):
    get_job = f"kubectl get job -o json {job_name}"

    # Wait until the whole job is finished:
    start = time.time()
    while True:
        current = time.time()
        if (timeout is not None) and (current - start) > timeout:
            raise RuntimeError(f"k8s: Job {job_name} timed out waiting.")
        result = json.loads(run_cmd(get_job))
        if ("failed" in result["status"]) and (result["status"]["failed"] >= result["spec"]["backoffLimit"]):
            raise RuntimeError(f"k8s: Job {job_name} failed.")
        if "succeeded" not in result["status"]:
            continue
        if result["status"]["succeeded"] == 1:
            break
        time.sleep(1)

    # Collect the names of the pods:
    get_pods = f"kubectl get pods --selector=job-name={job_name} --output=json"
    pods = json.loads(run_cmd(get_pods))
    pod_names = [p["metadata"]["name"] for p in pods["items"]]

    logs = {}
    for pod_name in pod_names:
        logs[pod_name] = json.loads(run_cmd(f"kubectl logs {pod_name}"))

    if delete:
        run_cmd(f"kubectl delete job {job_name}")

    if verbose:
        return logs
    else:
        assert(len(logs.values()) == 1)
        return list(logs.values())[0]


def map(func, iterable, image="jobs", imports=[], imagePullPolicy="Never", timeout=None, nowait=False, verbose=False):
    thunks = [lambda arg=i: func(arg) for i in iterable]

    job_names = [run(thunk, image=image, imports=imports, imagePullPolicy=imagePullPolicy) for thunk in thunks]

    if not nowait:
        if verbose:
            results = {j: wait(j, timeout=timeout, verbose=verbose) for j in job_names}
        else:
            results = [wait(j, timeout=timeout, verbose=verbose) for j in job_names]
        return results
    else:
        return job_names

