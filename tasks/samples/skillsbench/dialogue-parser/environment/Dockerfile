# Rebase mode: start from the coder_eval runtime image so the container inherits
# the framework runtime + org.coder-eval.version label, then re-apply the task's
# own setup on top. The upstream skillsbench base is python:3.12.8-slim.
FROM coder-eval-agent:latest

WORKDIR /app

# graphviz (python binding + system binary) for the agent's .dot visualization.
RUN pip install --no-cache-dir graphviz==0.20.3 pytest==8.3.4 pytest-json-ctrf==0.3.6 \
    && apt-get update \
    && apt-get install -y --no-install-recommends graphviz \
    && rm -rf /var/lib/apt/lists/*

# Task input, baked at the absolute path the prompt and verifier reference.
COPY script.txt /app/script.txt
