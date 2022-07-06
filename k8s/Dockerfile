# syntax=docker/dockerfile:1
FROM continuumio/miniconda3

# install kubectl
RUN apt update && \
      apt install -y curl && \
      curl -LO https://storage.googleapis.com/kubernetes-release/release/`curl -s https://storage.googleapis.com/kubernetes-release/release/stable.txt`/bin/linux/amd64/kubectl && \
      chmod +x ./kubectl && \
      mv ./kubectl /usr/local/bin/kubectl

ADD ./conda.yaml .
RUN conda env update -n base -f conda.yaml
WORKDIR /app
ENV PYTHONPATH "${PYTHONPATH}:/app"
ADD k8s.py .
CMD ["bash"]
