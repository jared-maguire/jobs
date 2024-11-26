#!/usr/bin/env python

import os
import sys
import builtins
from collections import Counter
from kubernetes import client, config

import dill as pickle
pickle.settings['recurse'] = True

import time
import base64
import json
import jinja2
import subprocess
import multiprocessing

import sk8s


def check_cluster_config():
    svc_acct = "sk8s" in subprocess.run("kubectl get serviceaccounts",
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
      serviceAccountName: {{serviceAccountName}}
      containers:
      - name: worker
        image: {{image}}
        imagePullPolicy: {{imagePullPolicy}}
        securityContext:
            privileged: {% if privileged %}true{% else %}false{% endif %}
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
              result = json.dumps(func())
              sys.stdout.write(result)
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
        imagePullPolicy=None,
        backoffLimit=0,
        serviceAccountName=None,
        privileged=False,
        name="job-{s}",
        test=False,
        dryrun=False,
        state=None,
        config=None,
        export_config=True,
        _map_helper=False):
    # Should do it this way, but having problems. Reverting for now:
    # job_template = importlib.resources.read_text("sk8s", "job_template.yaml")

    if config is None:
        config = sk8s.configs.load_config()

    if image is None:
        image = config["docker_image_prefix"] + "jobs"

    if imagePullPolicy is None:
        imagePullPolicy = config["docker_default_pull_policy"]

    if serviceAccountName is None:
        serviceAccountName = config["service_account_name"]

    if state is None:
        code = sk8s.util.serialize_func(lambda a=args: func(*a))
    else:
        code = sk8s.util.serialize_func(state.memoize(lambda a=args: func(*a)))

    if volumes.__class__ == str:
        volumes = {volumes: f"/mnt/{volumes}"}
    if volumes.__class__ == list:
        volumes = {name: f"/mnt/{name}" for name in volumes}

    if test:
        func_2 = pickle.loads(base64.b64decode(code))
        return func_2()

    t = jinja2.Template(job_template)
    s = sk8s.util.random_string(5)
    job = name=name.format(s=s)
    j = t.render(name=job,
                 code=code,
                 image=image,
                 requests=requests,
                 limits=limits,
                 volumes=volumes,
                 config=config if export_config else sk8s.configs.default_config,
                 imagePullPolicy=imagePullPolicy,
                 privileged=privileged,
                 backoffLimit=backoffLimit,
                 serviceAccountName=serviceAccountName)

    if _map_helper:
        return (job, j)
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


def get_job_statuses(job_names, namespace):
    """
    Retrieve the statuses of a list of Kubernetes jobs in a specified namespace.

    Parameters:
    - job_names (list of str): List of job names to retrieve statuses for.
    - namespace (str): The namespace where the jobs are located.

    Returns:
    - dict: A dictionary where each key is a job name and each value is its status.
    """
   
    if sk8s.in_pod():
        config.incluster_config.load_incluster_config()    
    else:
        config.load_kube_config()
    
    batch_v1 = client.BatchV1Api()

    # Fetch all jobs in the namespace
    jobs = batch_v1.list_namespaced_job(namespace=namespace).items

    # Collect status for specified jobs
    job_statuses = {}
    for job in jobs:
        if job.metadata.name in job_names:
            # Get the job status condition (e.g., Complete, Failed, Active)
            if job.status.conditions:
                job_status = job.status.conditions[0].type  # e.g., "Complete", "Failed"
            else:
                job_status = "Unknown" if not job.status.active else "Active"
            job_statuses[job.metadata.name] = job_status

    return job_statuses


def get_job_logs(job_name, namespace):
    if sk8s.in_pod():
        config.incluster_config.load_incluster_config()    
    else:
        config.load_kube_config()

    v1 = client.CoreV1Api()
    batch_v1 = client.BatchV1Api()
    
    try:
        # Get job to find its pods
        job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
        # Use the job's selector to list pods associated with it
        label_selector = ",".join([f"{k}={v}" for k, v in job.spec.selector.match_labels.items()])
        pods = v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector).items
        
            # Retrieve logs for a single pod that has completed successfully
        logs = ""
        for pod in pods:
            try:
                pod_status = v1.read_namespaced_pod_status(name=pod.metadata.name, namespace=namespace)
                if pod_status.status.phase == "Succeeded":
                    logs = v1.read_namespaced_pod_log(name=pod.metadata.name, namespace=namespace, _preload_content=False).data.decode("utf-8")
                    break
            except client.exceptions.ApiException as e:
                raise e

        return logs if logs else "null"
    except client.exceptions.ApiException as e:
        raise e


def get_jobs_logs(job_names, namespace):
    """
    Retrieve logs for a list of Kubernetes jobs in a specified namespace.

    Parameters:
    - job_names (list of str): List of job names to retrieve logs for.
    - namespace (str): The namespace where the jobs are located.

    Returns:
    - dict: A dictionary where each key is a job name and each value is the aggregated logs for that job.
    """

    if sk8s.in_pod():
        config.incluster_config.load_incluster_config()
    else:
        config.load_kube_config()

    v1 = client.CoreV1Api()
    batch_v1 = client.BatchV1Api()
    
    namespace = sk8s.get_current_namespace()

    with multiprocessing.Pool() as pool:
        job_logs = pool.starmap(get_job_logs, [(job, namespace) for job in job_names])
    
    return job_logs


def wait(jobs, timeout=None, verbose=False, delete=True, polling_interval=1.0):
    ns = sk8s.get_current_namespace()

    if jobs.__class__ == str:
        jobs = [jobs]

    while True:
        statuses = get_job_statuses(jobs, ns)
        if "Active" not in statuses.values(): break
        time.sleep(polling_interval)    

    if "Failed" in statuses.values():
        failures = [job for job, status in statuses.items() if status == "Failed"]
        raise RuntimeError(f"Jobs {' '.join(failures)} failed.")

    logs = get_jobs_logs(jobs, ns)

    results = list(builtins.map(json.loads, logs))

    if delete == True:
        subprocess.run(f"kubectl delete job {' '.join(jobs)}", shell=True, check=True, capture_output=True)

    if len(jobs) != 1:
        return results
    else:
        return results[0]


def map(func,
            iterable, 
            requests=dict(),
            limits=dict(),
            image=None,
            volumes={},
            imagePullPolicy=None,
            privileged=False,
            timeout=None,
            delete=True,
            asynchro=False,
            dryrun=False,
            verbose=False,
            chunk_size=100):
    thunks = [lambda arg=i: func(arg) for i in iterable]

    if dryrun:
        job_names = [run(thunk, image=image, requests=requests, limits=limits, imagePullPolicy=imagePullPolicy, privileged=privileged, volumes=volumes, dryrun=dryrun) for thunk in thunks]
        return job_names
    
    job_info = [run(thunk, image=image, requests=requests, limits=limits, imagePullPolicy=imagePullPolicy, privileged=privileged, volumes=volumes, dryrun=dryrun, _map_helper=True) for thunk in thunks]


    def chunk_job_info(job_info, chunk_size):
        for i in range(0, len(job_info), chunk_size):
            yield job_info[i:i + chunk_size]
            
    def submit_jobs_from_info(job_infos):
        job_names = [ji[0] for ji in job_infos]
        job_specs = [ji[1] for ji in job_infos]
        combined_job_spec = "\n---\n".join(job_specs)
        
        encoded_job_spec = combined_job_spec.encode("utf-8")
        if encoded_job_spec.__sizeof__() > 1000000:
            raise ValueError("Job spec too large. Please reduce the chunk size.")

        try:
            proc = subprocess.run("kubectl apply -f -",
                            shell=True,
                            input=combined_job_spec.encode("utf-8"),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=True)
        except subprocess.CalledProcessError as e:
            print("Error in submitting jobs:", e, flush=True)
            print("Command:" + e.cmd, "-----stdout-----", e.stdout, "-----stderr-----", e.stderr, flush=True, sep="\n")
            raise e

        return job_names    
  
    # Submit the chunks one by one
    chunk_names = builtins.map(submit_jobs_from_info, chunk_job_info(job_info, chunk_size))
    job_names = [name for chunk in chunk_names for name in chunk] 
    
    if asynchro or dryrun:
        return job_names
    else:
        return wait(job_names, timeout=timeout)


def starmap(func,
            iterable, 
            requests=dict(),
            limits=dict(),
            image=None,
            volumes={},
            imagePullPolicy=None,
            privileged=False,
            timeout=None,
            delete=True,
            asynchro=False,
            dryrun=False,
            verbose=False,
            chunk_size=100):
    thunks = [lambda arg=i: func(*arg) for i in iterable]

    if dryrun:
        job_names = [run(thunk, image=image, requests=requests, limits=limits, imagePullPolicy=imagePullPolicy, privileged=privileged, volumes=volumes, dryrun=dryrun) for thunk in thunks]
        return job_names
    
    job_info = [run(thunk, image=image, requests=requests, limits=limits, imagePullPolicy=imagePullPolicy, privileged=privileged, volumes=volumes, dryrun=dryrun, _map_helper=True) for thunk in thunks]


    def chunk_job_info(job_info, chunk_size):
        for i in range(0, len(job_info), chunk_size):
            yield job_info[i:i + chunk_size]
            
    def submit_jobs_from_info(job_infos):
        job_names = [ji[0] for ji in job_infos]
        job_specs = [ji[1] for ji in job_infos]
        combined_job_spec = "\n---\n".join(job_specs)
        
        encoded_job_spec = combined_job_spec.encode("utf-8")
        if encoded_job_spec.__sizeof__() > 1000000:
            raise ValueError("Job spec too large. Please reduce the chunk size.")

        try:
            proc = subprocess.run("kubectl apply -f -",
                            shell=True,
                            input=combined_job_spec.encode("utf-8"),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=True)
        except subprocess.CalledProcessError as e:
            print("Error in submitting jobs:", e, flush=True)
            print("Command:" + e.cmd, "-----stdout-----", e.stdout, "-----stderr-----", e.stderr, flush=True, sep="\n")
            print("---- job spec -----", combined_job_spec, "---- end job spec ----", flush=True, sep="\n")
            raise e

        return job_names    
  
    # Submit the chunks one by one
    chunk_names = builtins.map(submit_jobs_from_info, chunk_job_info(job_info, chunk_size))
    job_names = [name for chunk in chunk_names for name in chunk] 
    
    if asynchro or dryrun:
        return job_names
    else:
        return wait(job_names, timeout=timeout)