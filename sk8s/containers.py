import subprocess
import importlib
import importlib.resources
import jinja2
import sys


import sk8s
import sk8s.configs


def docker_push(tag):
    config = sk8s.configs.load_config()
    if config["ecr_create_repo_on_push"]:
        # parse the name of the repo from the tag
        repo_name = tag.split("/")[-1].split(":")[0]

        # create the repo 
        subprocess.run(f"aws ecr create-repository --repository-name {repo_name}", check=False, shell=True)
    subprocess.run(f"docker push {tag}", check=True, shell=True)


def docker_template(tag, ancestor=None, conda=[], pip=[], channels=[], additional_config="", push=True):
    #cwd = re.sub("^/C", "/c", re.sub("^", "/", re.sub(":", "", re.sub(r"\\", "/", os.getcwd()))))
    if ancestor is None:
        config = sk8s.configs.load_config()
        ancestor = config["docker_image_prefix"] + "jobs:latest"
    template = jinja2.Template(importlib.resources.read_text("sk8s", "Dockerfile.template"))
    rendered = template.render(conda=conda, pip=pip, channels=channels, additional_config=additional_config, ancestor=ancestor)
    return rendered

    
def docker_name(image_name):
    config = sk8s.configs.load_config()
    return config["docker_image_prefix"] + image_name


def docker_build(image_name=None, prefix=None, tag=None, ancestor=None, conda=[], pip=[], channels=[], push=None, dryrun=False, additional_config="", extra_options="", silent=False):
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

    rendered = docker_template(tag=tag, ancestor=ancestor, conda=conda, pip=pip, channels=channels, additional_config=additional_config, push=push)

    if dryrun:
        return rendered

    subprocess.run(f"docker build {extra_options} -t {tag} -f - .", input=rendered.encode("utf-8"), check=True, shell=True, capture_output=silent)
    if push: docker_push(tag)

    return tag


def docker_build_jobs_image(tag=None, push=None, dryrun=False, branch=None, dev_mode=False, extra_options=""):
    config = sk8s.configs.load_config()

    if tag is None:
        tag = config["docker_image_prefix"] + "jobs:latest"

    if push is None:
        push = config["docker_build_default_push_policy"]

    template = jinja2.Template(importlib.resources.read_text("sk8s", "Dockerfile.jobs"))

    # Get the version of python that we are using
    python_version = sys.version.split()[0]

    rendered = template.render(tag=tag, branch=branch, dev_mode=dev_mode, python_version=python_version)
    if dryrun:
        return rendered
    #cwd = os.getcwd()
    #os.chdir(sk8s.__path__[0])
    subprocess.run(f"docker build {extra_options} -t {tag} -f - .", input=rendered.encode("utf-8"), check=True, shell=True)
    #os.chdir(cwd)

    if push: docker_push(tag)
    return tag
