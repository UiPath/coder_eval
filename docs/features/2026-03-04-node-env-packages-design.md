# Node.js Environment Packages for Sandbox

**Related PR:** #38
**Date**: 2026-03-04
**Status**: Approved

## Problem

The uipath-flow evaluation tasks require `uipcli` (an npm package: `@uipath/uipcli`) to be available in the sandbox. Currently there is no mechanism to install npm packages during sandbox setup. Different versions of `uipcli` impact task performance, so we need version pinning and version reporting.

## Requirements

1. Specify npm packages (with versions) in task YAML
2. Install them during sandbox setup so the agent can use them
3. Capture installed versions in the evaluation report

## Design

### Data Model

New `NodeEnvConfig` in `models/sandbox.py`, mirroring `PythonEnvConfig`:

```python
class NodeEnvConfig(BaseModel):
    env_packages: list[str] = Field(
        default_factory=list,
        description="npm packages to install (e.g., '@uipath/uipcli@0.1.5')"
    )
```

New field on `SandboxConfig`:

```python
node: NodeEnvConfig | None = Field(
    default=None,
    description="Node.js environment config; set to enable npm package installation"
)
```

### Task YAML

```yaml
sandbox:
  driver: tempdir
  node:
    env_packages:
      - "@uipath/uipcli@0.1.5"
```

### Installation

New `Sandbox._install_node_packages()` method:
- Called from `_setup_tempdir()` after Python setup
- Runs `npm install <packages>` in the sandbox directory (tries bun first, falls back to npm)
- Creates `node_modules/.bin/` with CLI entry points
- After install, runs `npm list --json --depth=0` to capture installed versions into `self.installed_tool_versions: dict[str, str]`

### PATH Setup

In `Sandbox.run_command()`, prepend `node_modules/.bin` to PATH if it exists:

```python
node_bin = self.sandbox_dir / "node_modules" / ".bin"
if node_bin.exists():
    env["PATH"] = f"{node_bin}:{env['PATH']}"
```

### Version Reporting

- New `installed_tool_versions: dict[str, str]` attribute on `Sandbox`
- Orchestrator passes this into `EvaluationResult.environment_info["installed_tools"]`
- Already rendered in the markdown report's Environment section

## Files Changed

| File | Change |
|------|--------|
| `models/sandbox.py` | Add `NodeEnvConfig`, add `node` field to `SandboxConfig` |
| `sandbox.py` | Add `_install_node_packages()`, update `_setup_tempdir()`, update `run_command()` PATH, add `installed_tool_versions` |
| `orchestrator.py` | Pass `sandbox.installed_tool_versions` into `environment_info` |
| `tasks/uipath_flow/*/...yaml` | Add `node.env_packages` with uipcli |
| Tests | Unit tests for new functionality |
