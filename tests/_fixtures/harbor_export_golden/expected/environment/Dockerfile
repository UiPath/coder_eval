FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends coreutils && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY task.yaml /opt/coder-eval-task/task.yaml
