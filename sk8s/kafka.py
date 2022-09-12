
import jinja2
import subprocess
import sk8s


kafka_docker_instructions ="""
RUN apt-get install -y default-jre
RUN curl -O https://dlcdn.apache.org/kafka/3.2.1/kafka_2.13-3.2.1.tgz && tar -zxf kafka*.tgz
RUN echo "#!/usr/bin/env bash" > launch.sh
RUN echo "cd kafka_2.13-3.2.1 ; bin/zookeeper-server-start.sh config/zookeeper.properties & bin/kafka-server-start.sh config/server.properties & wait" >> launch.sh && chmod a+x launch.sh
CMD "/app/launch.sh"
"""

#"""
#kafka_2.13-3.2.1/bin/kafka-topics.sh --create --topic quickstart-events --bootstrap-server localhost:9092
#kafka_2.13-3.2.1/bin/kafka-topics.sh --describe --topic quickstart-events --bootstrap-server localhost:9092
#kafka_2.13-3.2.1/bin/kafka-console-producer.sh --topic quickstart-events --bootstrap-server localhost:9092
#kafka_2.13-3.2.1/bin/kafka-console-consumer.sh --topic quickstart-events --from-beginning --bootstrap-server localhost:9092
#"""


default_kafka_template = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{name}}
  labels:
    app.kubernetes.io/name: {{name}}
    app.kubernetes.io/component: backend
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: {{name}}
      app.kubernetes.io/component: backend
  replicas: 1
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {{name}}
        app.kubernetes.io/component: backend
    spec:
      containers:
      - name: {{name}}
        image: jobs-kafka
        imagePullPolicy: {{imagePullPolicy}}
        resources:
          requests:
            cpu: 100m
            memory: 100Mi
        ports:
        - containerPort: 9092
---
apiVersion: v1
kind: Service
metadata:
  name: {{name}}
  labels:
    app.kubernetes.io/name: {{name}}
    app.kubernetes.io/component: backend
spec:
  ports:
  - port: 9092
    targetPort: 9092
  selector:
    app.kubernetes.io/name: {{name}}
    app.kubernetes.io/component: backend
"""


def build_kafka_image(dryrun=False):
    return sk8s.docker_build("jobs-kafka", additional_config=kafka_docker_instructions, dryrun=dryrun)


def create_kafka(name=None, namespace=None, dryrun=False, template=default_kafka_template, imagePullPolicy=None, **kwargs):
    template = jinja2.Template(template)

    if name is None:
        string = sk8s.util.random_string(5)
        name = f"kafka-{string}"

    if namespace is None:
        namespace = sk8s.util.get_current_namespace()

    config = sk8s.configs.load_config()

    if imagePullPolicy is None:
        imagePullPolicy = config["docker_default_pull_policy"]

    template_args = kwargs.copy()
    template_args["name"] = name
    template_args["imagePullPolicy"] = imagePullPolicy

    kafka_yaml = template.render(**template_args)

    if dryrun:
      return kafka_yaml

    subprocess.run(f"kubectl apply -f - --namespace {namespace}",
                   shell=True,
                   input=kafka_yaml.encode("utf-8"),
                   check=True)

    url = f"{name}.{namespace}:9092"

    return dict(name=name, url=url)