
import jinja2
import subprocess
import sk8s


### This assumes helm for k8s is installed and in the path.

# https://github.com/bitnami/charts/tree/master/bitnami/kafka


def create_kafka(name=None, namespace=None, dryrun=False, **kwargs):
    if name is None:
        string = sk8s.util.random_string(5)
        name = f"{string}"

    if namespace is None:
        namespace = sk8s.util.get_current_namespace()

    subprocess.run(f"helm install {name} bitnami/kafka",
                   shell=True,
                   check=True)

    url = f"{name}-kafka-headless.{namespace}:9092"

    return dict(name=name + "-kafka", url=url)