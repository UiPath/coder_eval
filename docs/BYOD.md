# Bring Your Own Docker (BYOD)

The BYOD feature allows customers to use custom Docker images that extend the base `coder-eval-agent` image, enabling them to add custom dependencies and tools while maintaining the latest coder-eval codebase.

## Overview

By default, coder-eval uses the official `coder-eval-agent` image built via `make docker-image`. With BYOD, you can:

1. **Extend the base image** with custom dependencies
2. **Override the image tag** in your task configuration
3. **Run evaluations** with your custom image while leveraging all coder-eval features

## How It Works

### Architecture

- **`DockerDriverConfig.image`** field now defaults to `DEFAULT_IMAGE_TAG` (e.g., `coder-eval-agent:0.1.0`)
- **No environment variable override** needed—use task configuration instead
- **Simple precedence**: Task config → Default image

### Building a Custom Image

Create a `Dockerfile` in your repo:

```dockerfile
FROM coder-eval-agent:0.1.0

# Install custom dependencies
RUN apt-get update && apt-get install -y \
    custom-tool \
    another-dependency \
    && rm -rf /var/lib/apt/lists/*

# Add custom scripts or configuration
COPY custom-script.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/custom-script.sh
```

Build the image:

```bash
docker build -t my-custom-image:0.1.0 .
```

### Using a Custom Image in Tasks

Update your task YAML to reference the custom image:

```yaml
task_id: my_task
description: Task using custom Docker image

sandbox:
  driver: docker
  docker:
    image: my-custom-image:0.1.0  # Your custom image
    network: bridge
  limits:
    timeout: 300

initial_prompt: |
  Use the custom tools installed in my-custom-image to complete this task.

success_criteria:
  - type: file_exists
    path: output.txt
    description: Verify output file was created
```

## Example: BYOD Smoke Test

The codebase includes a smoke test that demonstrates BYOD:

**Task**: `tasks/byod_smoke_test.yaml`
- Uses custom image `byod-custom-image:0.1.0`
- Verifies the custom image was loaded via a marker file
- Tagged `smoke-pass` for CI/CD pipeline

**Template**: `templates/byod_smoke_test/Dockerfile`
- Extends `coder-eval-agent:0.1.0`
- Adds `/opt/byod_marker` to prove custom image was used

Run locally:

```bash
# Build the custom image
docker build -t byod-custom-image:0.1.0 templates/byod_smoke_test/

# Run the smoke test
set -a && source .env && set +a
coder-eval run tasks/byod_smoke_test.yaml
```

## Configuration Details

### DockerDriverConfig.image

- **Type**: `str` (not optional)
- **Default**: `DEFAULT_IMAGE_TAG` (e.g., `coder-eval-agent:0.1.0`)
- **Override**: Set in task YAML via `sandbox.docker.image`

### Example Configuration Layers

```yaml
# Default (no override)
sandbox:
  driver: docker
  # image defaults to coder-eval-agent:0.1.0

# Override at task level
sandbox:
  driver: docker
  docker:
    image: my-team/image:latest

# Override via CLI (future enhancement)
# coder-eval run task.yaml --docker-image custom:v1
```

## Best Practices

1. **Base image consistency**: Always extend from the same `coder-eval-agent` version as your host
   ```dockerfile
   FROM coder-eval-agent:0.1.0  # Match your host version
   ```

2. **Keep images lean**: Add only necessary dependencies
   ```dockerfile
   # Good: Clean up after install
   RUN apt-get update && apt-get install -y tool && rm -rf /var/lib/apt/lists/*

   # Avoid: Large intermediate layers
   RUN apt-get update
   RUN apt-get install -y tool
   ```

3. **Document custom tools**: Make it clear what you added
   ```dockerfile
   # Custom tools added for project X
   # - tool-a: used for step 1
   # - tool-b: used for step 2
   RUN apt-get install -y tool-a tool-b
   ```

4. **Version your images**: Use semver tags
   ```bash
   docker build -t my-company/evaluation-agent:1.0.0 .
   docker build -t my-company/evaluation-agent:latest .
   ```

5. **Test locally first**: Verify your image works before adding to CI
   ```bash
   docker build -t test-image:local .
   coder-eval run task.yaml  # Ensure task.yaml points to test-image:local
   ```

## Troubleshooting

### Image Not Found

**Error**: `docker: Error response from daemon: pull access denied`

**Solution**: Ensure the image exists and the tag is correct
```bash
# List available images
docker images | grep byod

# Rebuild if missing
docker build -t byod-custom-image:0.1.0 templates/byod_smoke_test/
```

### Version Mismatch

**Warning**: `Image coder-eval-agent:0.1.0 != host 0.1.0`

**Solution**: Rebuild the custom image after updating the base image
```bash
# Update base image
make docker-image

# Rebuild custom image
docker build --no-cache -t my-image:0.1.0 .
```

## CI/CD Integration

The BYOD feature is tested in the smoke-pass bucket of the e2e-smoke job:

1. Base image is built: `make docker-image`
2. BYOD template is built: `docker build templates/byod_smoke_test/`
3. Smoke test runs: `coder-eval run tasks/*.yaml --tags smoke-pass`
4. Results verified: `byod_smoke_test` should succeed

See `.github/workflows/pr-checks.yml` for the full pipeline.

## Related Files

- **Implementation**: `src/coder_eval/models/sandbox.py` (DockerDriverConfig)
- **Runner**: `src/coder_eval/isolation/docker_runner.py` (image selection)
- **Tests**: `tests/test_byod_feature.py`
- **Example Task**: `tasks/byod_smoke_test.yaml`
- **Example Dockerfile**: `templates/byod_smoke_test/Dockerfile`
- **CI/CD**: `.github/workflows/pr-checks.yml` (e2e-smoke, windows-smoke jobs)
