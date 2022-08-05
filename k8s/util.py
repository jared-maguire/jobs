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
import time

import k8s


def run_cmd(cmd):
    result = subprocess.run(cmd, check=True, shell=True, stdout=subprocess.PIPE).stdout
    return result


def serialize_func(func):
    code = base64.b64encode(pickle.dumps(func, byref=True, recurse=True)).decode("utf-8")
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
    cmd = "kubectl config view"
    config = get_k8s_config()
    return config["contexts"][0]["context"]["namespace"]   # I might really regret the hardcoded '[0]' here.


def get_pods_from_job(job):
    get_pods = f"kubectl get pods --selector=job-name={job} --output=json"
    pods = json.loads(run_cmd(get_pods))
    return pods


def get_pod_names_from_job(job):
    pods = get_pods_from_job(job)
    pod_names = [p["metadata"]["name"] for p in pods["items"]]
    return pod_names

def interactive_job(lifespan):
    # this bit is fun...

    def hang(lifespan=lifespan):
        import time
        time.sleep(lifespan)

    job = k8s.run(hang)

    pods = get_pods_from_job(job)
    phases = [pods["items"][i]["status"]["phase"]
              for i in range(len(pods["items"]))]
    

    print(f"Wating for {job} to get started...")
    while (phases[0] != "Running"):
        pods = get_pods_from_job(job)
        phases = [pods["items"][i]["status"]["phase"]
                  for i in range(len(pods["items"]))]
        time.sleep(1)
        print(phases)

    pod_names = get_pod_names_from_job(job)
    pod_name = pod_names[0]

    return os.system(f"kubectl exec --stdin --tty {pod_name} -- /bin/bash")