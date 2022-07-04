# syntax=docker/dockerfile:1
FROM continuumio/miniconda3
ADD ./conda.yaml .
RUN conda env update -n base -f conda.yaml
WORKDIR /app
ENV PYTHONPATH "${PYTHONPATH}:/app"
COPY . .
CMD ["bash"]
