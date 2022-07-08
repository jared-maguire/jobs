#!/usr/bin/env python

import os

import dill as pickle
pickle.settings['recurse'] = True

import threading
import time
import multiprocessing
from multiprocessing.pool import ThreadPool
import requests
import base64
import json
import sys
import functools
import string
import random
import jinja2
import subprocess
import inspect


def run_cmd(cmd):
    result = subprocess.run(cmd, check=True, shell=True, stdout=subprocess.PIPE).stdout
    return result


def serialize_func(func):
    code = base64.b64encode(pickle.dumps(func)).decode("utf-8")
    return code


def deserialize_func(code):
    func = pickle.loads(base64.b64decode(code))
    return func


def check_for_kwargs(func):
    varkw = inspect.getfullargspec(func).varkw
    return (varkw=='kwargs')


def random_string(length):
    letters = string.ascii_lowercase
    return ''.join(random.choice(letters) for i in range(length))


def run(func, *args, image="jobs", imports=[], deps=[], imagePullPolicy="Never", test=False):
    job_template = """apiVersion: batch/v1
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

          #func = pickle.loads(base64.b64decode("{{code}}"))
          func = k8s.deserialize_func("{{code}}")

          deps = {{deps}}
          if len(deps) != 0:
            inputs = {dep: k8s.wait(dep, delete=False) for dep in deps}
          else:
            inputs = dict()

          if k8s.check_for_kwargs(func):
            json.dump(func(inputs=inputs), sys.stdout)
          else:
            json.dump(func(), sys.stdout)
      restartPolicy: Never
  backoffLimit: 1
"""

    if check_for_kwargs(func):
        code = serialize_func(lambda a=args, **kwargs: func(*a, **kwargs))
    else:
        #thunks = [lambda arg=i: func(arg) for i in iterable]
        code = serialize_func(lambda a=args: func(*a))

    if test:
        func_2 = pickle.loads(base64.b64decode(code))
        return func_2()

    t = jinja2.Template(job_template)
    s = random_string(5)
    j = t.render(name=f"job-{s}", code=code, image=image, imports=imports, imagePullPolicy=imagePullPolicy, deps=deps)
    subprocess.run("kubectl apply -f -",
                   shell=True,
                   input=j.encode("utf-8"),
                   stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE,
                   check=True)
    return f"job-{s}"


def wait(job_name, timeout=None, verbose=False, delete=True):
    get_job = f"kubectl get job -o json {job_name}"

    # Wait until the whole job is finished:
    start = time.time()
    while True:
        current = time.time()
        if (timeout is not None) and (current - start) > timeout:
            return None
        result = json.loads(run_cmd(get_job))
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


def map(func, iterable, image="jobs", imports=[], deps=[], imagePullPolicy="Never", timeout=None, nowait=False, verbose=False):
    if check_for_kwargs(func):
        thunks = [lambda arg=i, **kwargs: func(arg, **kwargs) for i in iterable]
    else:
        thunks = [lambda arg=i: func(arg) for i in iterable]

    job_names = [run(thunk, image=image, imports=imports, imagePullPolicy=imagePullPolicy, deps=deps) for thunk in thunks]

    if not nowait:
        if verbose:
            results = {j: wait(j, timeout=timeout, verbose=verbose) for j in job_names}
        else:
            results = [wait(j, timeout=timeout, verbose=verbose) for j in job_names]
        return results
    else:
        return job_names

