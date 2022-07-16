# syntax=docker/dockerfile:1
FROM continuumio/miniconda3

# install kubectl
RUN apt update && \
      apt install -y curl && \
      curl -LO https://storage.googleapis.com/kubernetes-release/release/`curl -s https://storage.googleapis.com/kubernetes-release/release/stable.txt`/bin/linux/amd64/kubectl && \
      chmod +x ./kubectl && \
      mv ./kubectl /usr/local/bin/kubectl

ADD ./k8s/conda.yaml .
RUN conda env update -n base -f conda.yaml
WORKDIR /app
COPY . /app/module

# Installing in dev mode here is a hack to work around
# the fact that MANIFEST.in doesn't seem to be working in linux :-/
RUN cd /app/module && pip install -e .   

#ENV PYTHONPATH "${PYTHONPATH}:/app"
CMD ["bash"]
