import subprocess
import io
import base64
import dill as pickle
import inspect
import string
import random
import yaml
import json
import os
import re
import time
import importlib

import sk8s


def run_cmd(cmd, retries=1):
    n = 0
    while n < retries:
        result = subprocess.run(cmd, check=True, shell=True, stdout=subprocess.PIPE).stdout
        n += 1
        if n < retries: time.sleep(1)
    return result


def serialize_func(func):
    code = base64.b64encode(pickle.dumps(func, byref=True, recurse=False)).decode("utf-8")
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


def get_k8s_config():
    cmd = "kubectl config view"
    config = yaml.load(io.StringIO(subprocess.run(cmd, shell=True, check=True, stdout=subprocess.PIPE).stdout.decode("utf-8")), yaml.Loader)
    return config


def get_current_namespace():
    config = get_k8s_config()
    return subprocess.run("kubectl config view --minify --output 'jsonpath={..namespace}'", shell=True, check=True, stdout=subprocess.PIPE).stdout.decode("utf-8")


def set_namespace(ns):
    subprocess.run(f"kubectl config set-context --current --namespace={ns}")


def get_pods_from_job(job):
    get_pods = f"kubectl get pods --selector=job-name={job} --output=json"
    pods = json.loads(run_cmd(get_pods))
    return pods


def get_pod_names_from_job(job):
    pods = get_pods_from_job(job)
    pod_names = [p["metadata"]["name"] for p in pods["items"]]
    return pod_names

def interactive_job(image=None, volumes=None, service_account_name=None):
    # this bit is fun...

    def hang(lifespan):
        import time
        time.sleep(lifespan)

    job = sk8s.run(hang, 3600, image=image, volumes=volumes, serviceAccountName=service_account_name)

    pods = get_pods_from_job(job)
    phases = [pods["items"][i]["status"]["phase"]
              for i in range(len(pods["items"]))]
    
    print(f"Wating for {job} to get started...")
    while (len(phases) == 0) or (phases[0] != "Running"):
        pods = get_pods_from_job(job)
        phases = [pods["items"][i]["status"]["phase"]
                  for i in range(len(pods["items"]))]
        time.sleep(1)
        print(",".join(phases), "                 ", end="\r")

    pod_names = get_pod_names_from_job(job)
    pod_name = pod_names[0]

    print(pod_name)

    return os.system(f"kubectl exec --stdin --tty {pod_name} -- bash")


def wipe_namespace(namespace=None):
    namespace_arg = f"-n {namespace}" if namespace is not None else ""

    resources = []
    for line in run_cmd(f"kubectl get all {namespace_arg}").decode("utf-8").split("\n"):
        if re.match("^\s*$", line): continue
        if re.match("^NAME", line): continue
        resources.append(line.strip().split()[0])

    resources_string = " ".join(resources)
    run_cmd(f"kubectl delete {resources_string}")
