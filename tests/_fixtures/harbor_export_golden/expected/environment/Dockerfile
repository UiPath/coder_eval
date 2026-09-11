FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends coreutils && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY agent_task.yaml /opt/coder-eval-task/task.yaml
