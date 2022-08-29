import subprocess
import importlib
import importlib.resources
from tkinter import E
import jinja2
import os


import sk8s
import sk8s.configs


def docker_push(tag):
    subprocess.run(f"docker push {tag}", check=True, shell=True)


def docker_template(tag, ancestor=None, conda=[], pip=[], channels=[], push=True):
    #cwd = re.sub("^/C", "/c", re.sub("^", "/", re.sub(":", "", re.sub(r"\\", "/", os.getcwd()))))
    if ancestor is None:
        config = sk8s.configs.load_config()
        ancestor = config["docker_image_prefix"] + "jobs"
    template = jinja2.Template(importlib.resources.read_text("sk8s", "Dockerfile.template"))
    rendered = template.render(conda=conda, pip=pip, channels=channels, ancestor=ancestor)
    return rendered


def docker_build(image_name=None, prefix=None, tag=None, ancestor=None, conda=[], pip=[], channels=[], push=None, dryrun=False, extra_options=""):
    config = sk8s.configs.load_config()

    if push is None:
        push = sk8s.configs.load_config()["docker_build_default_push_policy"]

    # this bit gets a little complex
    if tag is None:
        assert(image_name is not None)
        if prefix is None: prefix = config["docker_image_prefix"]
        tag = prefix + image_name

    if image_name is None:
        assert(tag is not None)

    rendered = docker_template(tag=tag, ancestor=ancestor, conda=conda, pip=pip, channels=channels, push=push)
    if dryrun:
        return rendered

    subprocess.run(f"docker build {extra_options} -t {tag} -f - .", input=rendered.encode("utf-8"), check=True, shell=True)
    if push: docker_push(tag)

    return tag


def docker_build_jobs_image(tag=None, push=None, dryrun=False, branch=None, extra_options=""):
    config = sk8s.configs.load_config()

    if tag is None:
        tag = config["docker_image_prefix"] + "jobs"

    if push is None:
        push = config["docker_build_default_push_policy"]

    template = jinja2.Template(importlib.resources.read_text("sk8s", "Dockerfile.jobs"))
    rendered = template.render(tag=tag, branch=branch)
    if dryrun:
        return rendered
    #cwd = os.getcwd()
    #os.chdir(sk8s.__path__[0])
    subprocess.run(f"docker build {extra_options} -t {tag} -f - .", input=rendered.encode("utf-8"), check=True, shell=True)
    #os.chdir(cwd)

    if push: docker_push(tag)
    return tag