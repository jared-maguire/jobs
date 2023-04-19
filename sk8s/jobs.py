#!/usr/bin/env python

import os
import sys

import dill as pickle
pickle.settings['recurse'] = True

import time
import base64
import json
import jinja2
import subprocess

import sk8s


def check_cluster_config():
    svc_acct = "internal-kubectl" in subprocess.run("kubectl get serviceaccounts",
                                                    check=True,
                                                    shell=True,
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
      volumes:
      {% for volume in volumes %}
      - name: {{volume}}
        persistentVolumeClaim:
          claimName: {{volume}}
      {% endfor %}
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
          import sk8s

          func = sk8s.deserialize_func("{{code}}")

          config = {{config}}
          sk8s.configs.save_config(config)

          json.dump(func(), sys.stdout)

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
        {%- for volume in volumes %}
        - mountPath: "/mnt/{{volume}}"
          name: {{volume}}
        {%- endfor %}
        {%- endif %}

      restartPolicy: Never
  backoffLimit: 1
"""


def run(func, *args,
        image=None,
        volumes=[],
        requests=dict(),
        limits=dict(),
        nowait=True,
        timeout=None,
        job_template=default_job_template,
        imagePullPolicy=None,
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

    if test:
        func_2 = pickle.loads(base64.b64decode(code))
        return func_2()

    t = jinja2.Template(job_template)
    s = sk8s.util.random_string(5)
    j = t.render(name=f"job-{s}",
                 code=code,
                 image=image,
                 requests=requests,
                 limits=limits,
                 volumes=volumes,
                 config=config if export_config else sk8s.configs.default_config,
                 imagePullPolicy=imagePullPolicy)

    if dryrun:
        return j

    subprocess.run("kubectl apply -f -",
                   shell=True,
                   input=j.encode("utf-8"),
                   stdout=subprocess.PIPE,
                   check=True)
                   #stderr=subprocess.PIPE,

    job = f"job-{s}"

    if nowait:
        return f"job-{s}"
    else:
        return wait(job, timeout=timeout)


def logs(job_name, decode=True):
    # Collect the names of the pods:
    get_pods = f"kubectl get pods --selector=job-name={job_name} --output=json"
    pods = json.loads(sk8s.util.run_cmd(get_pods))
    pod_names = [p["metadata"]["name"] for p in pods["items"]]

    logs = {}
    for pod_name in pod_names:
        if decode:
            try:
                pod_logs_text = sk8s.util.run_cmd(f"kubectl logs {pod_name}")
                logs[pod_name] = json.loads(pod_logs_text)
            except json.JSONDecodeError as e:
                print("job failed with error:", pod_logs_text, sep="\n", flush=True)
        else:
            logs[pod_name] = sk8s.util.run_cmd(f"kubectl logs {pod_name}").decode("utf-8")
    return logs


def wait(job_name, timeout=None, verbose=False, delete=True):
    get_job = f"kubectl get job -o json {job_name}"

    # Wait until the whole job is finished:
    start = time.time()
    while True:
        current = time.time()
        if (timeout is not None) and (current - start) > timeout:
            raise RuntimeError(f"k8s: Job {job_name} timed out waiting.")
        result = json.loads(sk8s.util.run_cmd(get_job))
        if ("failed" in result["status"]) and (result["status"]["failed"] >= result["spec"]["backoffLimit"]):
            #log_data = logs(job_name, decode=False)
            #print(f"🔥sk8s: job {job_name} failed.")
            #for pod_name, log in log_data.items():
            #    print(f"---- {pod_name} ----:", log, sep="\n") #, file=sys.stderr)
            raise RuntimeError(f"k8s: Job {job_name} failed.")
        if "succeeded" not in result["status"]:
            continue
        if result["status"]["succeeded"] == 1:
            break
        time.sleep(1)

    log_text = logs(job_name)

    if delete:
        sk8s.util.run_cmd(f"kubectl delete job {job_name}")

    if verbose:
        return log_text
    else:
        assert(len(log_text.values()) == 1)
        return list(log_text.values())[0]


def map(func,
        iterable, 
        requests=dict(),
        limits=dict(),
        image=None,
        imagePullPolicy=None,
        timeout=None,
        nowait=False,
        verbose=False):
    thunks = [lambda arg=i: func(arg) for i in iterable]

    job_names = [run(thunk, image=image, requests=requests, limits=limits, imagePullPolicy=imagePullPolicy) for thunk in thunks]

    if not nowait:
        if verbose:
            results = {j: wait(j, timeout=timeout, verbose=verbose) for j in job_names}
        else:
            results = [wait(j, timeout=timeout, verbose=verbose) for j in job_names]
        return results
    else:
        return job_names

