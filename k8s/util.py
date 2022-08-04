import subprocess
import io
import base64
import dill as pickle
import inspect
import string
import random
import yaml


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
    cmd = "kubectl config view --minify"
    config = yaml.load(io.StringIO(subprocess.run(cmd, shell=True, check=True, stdout=subprocess.PIPE).stdout.decode("utf-8")), yaml.Loader)
    return config


def get_current_namespace():
    cmd = "kubectl config view --minify"
    config = get_k8s_config()
    return config["contexts"][0]["context"]["namespace"]   # I might really regret the hardcoded '[0]' here.