# Custom BYOD image for smoke testing
# Extends the base coder-eval-agent image with custom dependencies/markers.
# `latest` tracks whatever `make docker-image` just built, so this template
# doesn't break on package version bumps.
FROM coder-eval-agent:latest

# Add a marker file to verify this custom image was actually used
RUN echo "BYOD custom image loaded" > /opt/byod_marker

# Example: Add custom tools or dependencies here
# RUN apt-get update && apt-get install -y custom-tool

LABEL org.coder-eval.byod="true" \
      org.coder-eval.smoke-test="byod"
