import subprocess
import importlib
import importlib.resources
import jinja2
import os
import re


def docker_push(tag):
    subprocess.run(f"docker push {tag}", check=True, shell=True)


def docker_template(tag, ancestor=None, conda=[], pip=[], channels=[], push=True):
    cwd = re.sub("^/C", "/c", re.sub("^", "/", re.sub(":", "", re.sub(r"\\", "/", os.getcwd()))))
    template = jinja2.Template(importlib.resources.read_text("k8s", "Dockerfile.template"))
    rendered = template.render(conda=conda, pip=pip, cwd=cwd, channels=channels)
    return rendered


def docker_build(tag, ancestor=None, conda=[], pip=[], channels=[], push=True, dryrun=False):
    rendered = docker_template(tag, ancestor, conda, pip, channels, push)
    if dryrun:
        return rendered
    subprocess.run(f"docker build -t {tag} -f - .", input=rendered.encode("utf-8"), check=True, shell=True)
    if push: docker_push(tag)
    return tag


