import subprocess
import importlib
import importlib.resources
import jinja2
import os


import k8s


def docker_push(tag):
    subprocess.run(f"docker push {tag}", check=True, shell=True)


def docker_template(tag, ancestor=None, conda=[], pip=[], channels=[], push=True):
    #cwd = re.sub("^/C", "/c", re.sub("^", "/", re.sub(":", "", re.sub(r"\\", "/", os.getcwd()))))
    template = jinja2.Template(importlib.resources.read_text("k8s", "Dockerfile.template"))
    rendered = template.render(conda=conda, pip=pip, channels=channels, ancestor=ancestor)
    return rendered


def docker_build(tag, ancestor=None, conda=[], pip=[], channels=[], push=None, dryrun=False, extra_options=""):
    if push is None:
        push = k8s.configs.load_config()["docker_build_default_push_policy"]

    rendered = docker_template(tag=tag, ancestor=ancestor, conda=conda, pip=pip, channels=channels, push=push)
    if dryrun:
        return rendered

    subprocess.run(f"docker build {extra_options} -t {tag} -f - .", input=rendered.encode("utf-8"), check=True, shell=True)
    if push: docker_push(tag)

    return tag


def docker_build_jobs_image(tag="jobs", push=None, dryrun=False, extra_options=""):
    if push is None:
        push = k8s.configs.load_config()["docker_build_default_push_policy"]

    template = jinja2.Template(importlib.resources.read_text("k8s", "Dockerfile.jobs"))
    rendered = template.render(tag=tag)
    if dryrun:
        return rendered
    cwd = os.getcwd()
    os.chdir(k8s.__path__[0])
    subprocess.run(f"docker build {extra_options} -t {tag} -f - .", input=rendered.encode("utf-8"), check=True, shell=True)
    os.chdir(cwd)
    if push: docker_push(tag)
    return tag