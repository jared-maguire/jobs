#!/usr/bin/env python

import os
import sys
import builtins
from collections import Counter
from kubernetes import client, config
import pandas as pd

import dill as pickle
pickle.settings['recurse'] = True

import time
import base64
import json
import jinja2
import subprocess
import multiprocessing

from concurrent.futures import ThreadPoolExecutor
import functools

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
    metadata:
      annotations:
        cluster-autoscaler.kubernetes.io/safe-to-evict: "{% if safe_to_evict %}true{% else %}false{% endif %}"
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

      restartPolicy: {{restartPolicy}}
  backoffLimit: {{backoffLimit}}
  ttlSecondsAfterFinished: {{ttlSecondsAfterFinished}}
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
        restartPolicy="Never",
        ttlSecondsAfterFinished="null",
        safe_to_evict=False,
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
                 ttlSecondsAfterFinished=ttlSecondsAfterFinished,
                 safe_to_evict=safe_to_evict,
                 restartPolicy=restartPolicy,
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


def get_job_statuses(namespace=None):

    if sk8s.util.in_pod():
        config.load_incluster_config()
    else:
        config.load_kube_config()

    batch_v1 = client.BatchV1Api()

    all_jobs = batch_v1.list_job_for_all_namespaces()

    results = []
    for job in all_jobs.items:
        name = job.metadata.name
        succeeded = job.status.succeeded or 0
        failed = job.status.failed or 0
        active = job.status.active or 0
        _namespace = job.metadata.namespace
        results.append(dict(job=name, namespace=_namespace, succeeded=succeeded, failed=failed, active=active))
    if namespace is None:
        return pd.DataFrame(results)
    else:
        return pd.DataFrame([r for r in results if r['namespace'] == namespace])
    

def get_completed_pod_from_jobs(jobs, namespace=None):

    if sk8s.util.in_pod():
        config.load_incluster_config()
    else:
        config.load_kube_config()

    core_v1 = client.CoreV1Api()
    
    if namespace is None:
        namespace = sk8s.util.get_current_namespace()

    pods = core_v1.list_namespaced_pod(namespace=namespace)

    job_set = set(jobs)

    results = dict()
    for pod in pods.items:
        job = pod.metadata.owner_references[0].name
        if job not in job_set:
            continue
        assert(pod.status.phase == 'Succeeded')
        results[job] = pod.metadata.name

    return results


def fetch_pod_results(pod_name, namespace=None):
    if namespace is None:
        namespace = sk8s.util.get_current_namespace()

    try:
        v1 = client.CoreV1Api()
        return json.loads(v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, _preload_content=False).data.decode("utf-8"))
    except Exception as e:
        print(f"Failed to fetch logs for pod {pod_name}: {e}")
        raise


def get_jobs_results(jobs, namespace=None):
    if sk8s.util.in_pod():
        config.load_incluster_config()
    else: 
        config.load_kube_config()
        
    core_v1 = client.CoreV1Api()

    if namespace is None:
        namespace = sk8s.util.get_current_namespace()

    pods = get_completed_pod_from_jobs(jobs, namespace)

    with ThreadPoolExecutor(max_workers=100) as executor:
        logs = executor.map(functools.partial(fetch_pod_results, namespace=namespace), pods.values())

    results = dict()
    for job, log in zip(pods.keys(), logs):
        results[job] = log

    final = []
    for job in jobs:
        final.append(results[job])

    return final


def wait(jobs, timeout=None, verbose=False, delete=True, polling_interval=1.0):
    ns = sk8s.get_current_namespace()

    if jobs.__class__ == str:
        jobs = [jobs]

    while True:
        status = get_job_statuses(ns)
        status = status.loc[status['job'].isin(jobs)]
        
        if status['active'].sum() > 0:
            time.sleep(polling_interval)
            continue
        else:
            break

    if status.failed.sum() > 0:
        failures = status.loc[status['failed'] > 0, 'job'].tolist()
        raise RuntimeError(f"Jobs {' '.join(failures)} failed.")

    results = get_jobs_results(jobs, ns)

    if delete == True:
        with ThreadPoolExecutor(max_workers=1000) as executor:
            # chunk jobs into groups of ten
            job_chunks = [jobs[i:i + 10] for i in range(0, len(jobs), 10)]
            cmds = [f"kubectl delete job {' '.join(chunk)}" for chunk in job_chunks]
            executor.map(functools.partial(subprocess.run, shell=True, check=True, capture_output=True), 
                         cmds)

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
    
    job_info = [run(thunk, image=image, requests=requests, limits=limits, volumes=volumes, imagePullPolicy=imagePullPolicy, privileged=privileged, dryrun=dryrun, _map_helper=True) for thunk in thunks]

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
  
    with ThreadPoolExecutor(max_workers=1000) as executor:
        chunk_names = executor.map(submit_jobs_from_info, list(chunk_job_info(job_info, chunk_size)))

    job_names = [name for chunk in chunk_names for name in chunk] 
    
    if asynchro or dryrun:
        return job_names
    else:
        return wait(job_names, timeout=timeout, delete=delete)


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
    
    job_info = [run(thunk, image=image, requests=requests, limits=limits, volumes=volumes, imagePullPolicy=imagePullPolicy, privileged=privileged, dryrun=dryrun, _map_helper=True) for thunk in thunks]

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
    with ThreadPoolExecutor(max_workers=1000) as executor:
        chunk_names = executor.map(submit_jobs_from_info, list(chunk_job_info(job_info, chunk_size)))   

    job_names = [name for chunk in chunk_names for name in chunk] 
    
    if asynchro or dryrun:
        return job_names
    else:
        return wait(job_names, timeout=timeout, delete=delete)


################
# Chunked versions of map and starmap -- for very large sets of jobs

def chunked_map(func, iterable, size=100, asynchro=False, **kwargs):
    thunks = [lambda arg=arg: func(arg) for arg in iterable]

    def thunker(thunks):
        results = [thunk() for thunk in thunks]
        return results

    thunk_chunks = [thunks[i:i + size] for i in range(0, len(thunks), size)]

    #chunked_results = [thunker(chunk) for chunk in thunk_chunks]
 
    def flatten(l):
        return [item for sublist in l for item in sublist]

    if asynchro:
        chunked_jobs = sk8s.map(thunker, thunk_chunks, asynchro=True, **kwargs)
        return chunked_jobs
    else:
        chunked_jobs = sk8s.map(thunker, thunk_chunks, asynchro=True, **kwargs)
        results = sk8s.chunked_wait(chunked_jobs)
        return results
    

def chunked_starmap(func, iterable, size=100, asynchro=False, **kwargs):
    thunks = [lambda arg=arg: func(*arg) for arg in iterable]

    def thunker(thunks):
        results = [thunk() for thunk in thunks]
        return results

    thunk_chunks = [thunks[i:i + size] for i in range(0, len(thunks), size)]

    #chunked_results = [thunker(chunk) for chunk in thunk_chunks]
 
    def flatten(l):
        return [item for sublist in l for item in sublist]

    if asynchro:
        chunked_jobs = sk8s.map(thunker, thunk_chunks, asynchro=True, **kwargs)
        return chunked_jobs
    else:
        chunked_jobs = sk8s.map(thunker, thunk_chunks, asynchro=True, **kwargs)
        results = sk8s.chunked_wait(chunked_jobs)
        return results


def chunked_wait(chunked_jobs, **kwargs):
    resultses = sk8s.wait(chunked_jobs, **kwargs)

    assert(resultses.__class__ == list or resultses.__class__ == tuple)

    if resultses[0].__class__ != list:
        resultses = [resultses]    

    def flatten(l):
        return [item for sublist in l for item in sublist]
    
    return flatten(resultses)
