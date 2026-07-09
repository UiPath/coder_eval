# Example task image for the dockerfile-build feature.
#
# MUST extend the framework image so the container inherits the coder-eval
# runtime + entrypoint (a bare `FROM ubuntu` fails at `docker run`). Build the
# base first:  make docker-image
FROM coder-eval-agent:latest

# 1) Build arg (from sandbox.docker.build.args) -> baked into a marker file,
#    which a success criterion verifies.
ARG GREETING="default greeting"
RUN echo "$GREETING" > /opt/greeting.txt

# 2) BuildKit secret (from sandbox.docker.build.secrets) -> mounted only for this
#    RUN, never baked into a layer. We record whether it was available (NOT its
#    value), so the example builds even when DEMO_SECRET is unset on the host.
RUN --mount=type=secret,id=demo_secret \
    if [ -s /run/secrets/demo_secret ]; then \
        echo "secret-available" > /opt/secret_check.txt; \
    else \
        echo "secret-not-set" > /opt/secret_check.txt; \
    fi
