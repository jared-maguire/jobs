import subprocess
import importlib
import importlib.resources
import jinja2
import os
import re


def docker_push(tag):
    subprocess.run(f"docker push {tag}", check=True, shell=True)


def docker_template(tag, ancestor=None, conda=[], pip=[], push=True):
    cwd = re.sub("^/C", "/c", re.sub("^", "/", re.sub(":", "", re.sub(r"\\", "/", os.getcwd()))))
    template = jinja2.Template(importlib.resources.read_text("k8s", "Dockerfile.template"))
    rendered = template.render(conda=conda, pip=pip, cwd=cwd)
    return rendered


def docker_build(tag, ancestor=None, conda=[], pip=[], push=True):
    rendered = docker_template(tag, ancestor, conda, pip, push)
    print(rendered, flush=True)
    subprocess.run(f"docker build -t {tag} -f - .", input=rendered.encode("utf-8"), check=True, shell=True)
    if push: docker_push(tag)
    return tag



