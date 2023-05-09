#!/usr/bin/env python

import os
import sys
from collections import Counter

import dill as pickle
pickle.settings['recurse'] = True

import time
import base64
import json
import jinja2
import subprocess
import sys
import re
import numpy
import multiprocessing

import sk8s
import sk8s.datastore


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
      {% for volume in volumes.keys() %}
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

      restartPolicy: Never
  backoffLimit: {{backoffLimit}}
"""

        #- mountPath: "/mnt/{{volume}}"

def run(func, *args,
        image=None,
        volumes={},
        requests=dict(),
        limits=dict(),
        asynchro=True,
        timeout=None,
        job_template=default_job_template,
        results_url=None,
        imagePullPolicy=None,
        backoffLimit=0,
        name="job-{s}",
        test=False,
        dryrun=False,
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

    s = sk8s.util.random_string(5)
    job = name=name.format(s=s)

    if results_url is not None:
        def logging_wrapper(func, args, job_name=job, results_url=results_url):
            import sk8s.datastore
            result = func(*args)
            sk8s.datastore.put(results_url, job_name, result)
            return result
        final_func = lambda *args: logging_wrapper(func, args, job_name=job, results_url=results_url)
    else:
        final_func = func

    code = sk8s.util.serialize_func(lambda a=args: final_func(*a))

    if volumes.__class__ == str:
        volumes = {volumes: f"/mnt/{volumes}"}
    if volumes.__class__ == list:
        volumes = {name: f"/mnt/{name}" for name in volumes}

    if test:
        func_2 = pickle.loads(base64.b64decode(code))
        return func_2()

    t = jinja2.Template(job_template)

    j = t.render(name=job,
                 code=code,
                 image=image,
                 requests=requests,
                 limits=limits,
                 volumes=volumes,
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

    if asynchro:
        return job
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
                pod_logs_text = sk8s.util.run_cmd(f"kubectl logs {pod_name}", retries=2)
                logs[pod_name] = json.loads(pod_logs_text)
            except json.JSONDecodeError as e:
                print("job failed with error:", pod_logs_text, sep="\n", flush=True)
        else:
            logs[pod_name] = sk8s.util.run_cmd(f"kubectl logs {pod_name}").decode("utf-8")
    return logs


def wait2(jobs, results_url, timeout=None, verbose=False, delete=True, polling_interval=1.0):

    # if we're not in a pod, then we need to set up a port forward.
    if "KUBERNETES_SERVICE_HOST" not in os.environ:
        # need a port forward
        service_name = re.search("://(.*?)\.", results_url).groups()[0]
        remote_port = 5000
        local_port = int(numpy.random.rand() * 10000 + 10000)
        cmd = f"kubectl port-forward service/{service_name} {local_port}:{remote_port}"
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        local_url = f"http://localhost:{local_port}/"
        original_url = results_url
        results_url = local_url

    done = False
    while not done:
        results = sk8s.datastore.get(results_url)
        r = [results.get(job, None) for job in jobs]
        if None not in r:
            done = True
    
    for job in jobs:
        sk8s.util.run_cmd(f"kubectl delete job {job}")

    if "KUBERNETES_SERVICE_HOST" not in os.environ:
        # shut down the port forward
        proc.terminate()
        proc.wait()

        # shut down the service
        sk8s.util.run_cmd(f"kubectl delete service {service_name}")
        sk8s.util.run_cmd(f"kubectl delete deployment {service_name}")

    return r


def wait(jobs, timeout=None, verbose=False, delete=True, polling_interval=1.0):
    def get_job_status_json(jobs):
        stdout = subprocess.run(f"kubectl get jobs {' '.join(jobs)} -o json --ignore-not-found", 
                            check=True, shell=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8").stdout
        try:
            data = json.loads(stdout)
            if data["kind"] == "Job":
                return dict(kind="List", items=[data])
            else: 
                return data
        except json.JSONDecodeError:
            return dict(kind="List", items=[])

    def check_job_status_json(data):
        assert(data["kind"] == "Job")
        if ("failed" in data["status"]) and (data["status"]["failed"] >= data["spec"]["backoffLimit"]):
            return "failed"
        if "succeeded" not in data["status"]:
            return "running"
        if data["status"]["succeeded"] == 1:
            return "success"

    def cleanup_job(job_name, delete=True, decode=True):
        log_text = sk8s.logs(job_name, decode=decode)
        if delete:
            sk8s.util.run_cmd(f"kubectl delete job {job_name}")
        if decode:
            return list(log_text.values())[0]
        else:
            return log_text

    if jobs.__class__ == str:
        jobs = [jobs]

    data = get_job_status_json(jobs)

    results = {d["metadata"]["name"]: check_job_status_json(d) for d in data["items"]}

    actual_results = dict()  # nice variable name doofus 😜

    failures = set()
    while True:
        data = get_job_status_json(jobs)
        results = {d["metadata"]["name"]: check_job_status_json(d) for d in data["items"]}
        if "running" not in results.values(): break

        for j, s in results.items():
            if j in actual_results.keys(): continue
            if s == "success":
                val = cleanup_job(j, delete=delete)
                actual_results[j] = val
            elif s == "failed":
                val = cleanup_job(j, delete=False, decode=False)
                actual_results[j] = val
                failures.add(j)
            elif s == "running":
                continue
            else:
                assert(False)

        time.sleep(1)

    # Now clean up whatever's left
    data = get_job_status_json(jobs)
    results = {d["metadata"]["name"]: check_job_status_json(d) for d in data["items"]}

    for j, s in results.items():
        if j in actual_results.keys(): continue
        if s == "success":
            val = cleanup_job(j, delete=delete)
            actual_results[j] = val
        elif s == "failed":
            val = cleanup_job(j, delete=False, decode=False)
            actual_results[j] = val
            failures.add(j)
        else:
            assert(False)

    data = get_job_status_json(jobs)
    results = {d["metadata"]["name"]: check_job_status_json(d) for d in data["items"]}

    if len(failures) != 0:
        raise RuntimeError(f"Jobs {' '.join(failures)} failed.")

    if len(jobs) != 1:
        return [actual_results[job] for job in jobs]
    else:
        return actual_results[jobs[0]]


def map2(func,
        iterable, 
        requests=dict(),
        limits=dict(),
        image=None,
        volumes={},
        imagePullPolicy=None,
        timeout=None,
        delete=True,
        asynchro=False,
        verbose=False):

    thunks = [lambda arg=i: func(arg) for i in iterable]

    def datastore_service():
        import sk8s.datastore as ds
        datastore = ds.Datastore()
        datastore.run()

    results_url = sk8s.services.service(datastore_service)
    time.sleep(5) # TODO: obviously, this is awful

    job_names = [run(thunk,
                     image=image,
                     requests=requests,
                     limits=limits,
                     imagePullPolicy=imagePullPolicy,
                     results_url=results_url,
                     volumes=volumes)
                 for thunk in thunks]
    if asynchro:
        return results_url, job_names
    else:
        return wait2(job_names, results_url, timeout=timeout)


def map(func,
        iterable, 
        requests=dict(),
        limits=dict(),
        image=None,
        volumes={},
        imagePullPolicy=None,
        timeout=None,
        delete=True,
        asynchro=False,
        verbose=False):
    thunks = [lambda arg=i: func(arg) for i in iterable]

    job_names = [run(thunk,
                     image=image,
                     requests=requests,
                     limits=limits,
                     imagePullPolicy=imagePullPolicy,
                     volumes=volumes)
                 for thunk in thunks]
    if asynchro:
        return job_names
    else:
        return wait(job_names, timeout=timeout)