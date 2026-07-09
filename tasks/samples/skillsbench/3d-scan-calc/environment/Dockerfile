# Rebase mode: start from the coder_eval runtime image so the container inherits
# the framework runtime + org.coder-eval.version label, then re-apply the task's
# own setup on top. The upstream skillsbench base is ubuntu:24.04.
FROM coder-eval-agent:latest

WORKDIR /root

# Bake the verifier's test-runner deps so scoring needs no network at run time.
RUN pip install --no-cache-dir pytest==8.4.1 pytest-json-ctrf==0.3.5

# Task inputs, baked at the absolute paths the prompt and verifier reference.
COPY scan_data.stl /root/scan_data.stl
COPY material_density_table.md /root/material_density_table.md
