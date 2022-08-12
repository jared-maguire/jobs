import k8s
import os
import json
import subprocess

import k8s.util


def get_homedir():
    return os.environ.get("HOME", os.environ.get("USERPROFILE", None))


default_fname = get_homedir() + f"/.sk8s/config.json"


default_config = dict(
                      docker_image_prefix="",
                     ) 


def load_config(fname=default_fname, create=True):
    if not os.path.exists(fname):
        save_config(default_config, fname)

    with open(fname) as fp:
        config = json.load(fp)
    return config


def save_config(config, fname=default_fname):
    dir = os.path.dirname(fname)
    if not os.path.exists(dir):
        os.mkdir(dir)
    with open(fname, "w") as fp:
        json.dump(config, fp)
    k8s.util.deep_reload()


def reset_config(fname=default_fname):
    save_config(default_config, fname)