# mock-cli-bins

Sample template directory for the `SandboxConfig.mock_path_dirs` feature.

The sibling task YAML references this folder via:

```yaml
sandbox:
  template_sources:
    - type: template_dir
      path: "./mock-cli-bins"
  mock_path_dirs:
    - "mocks"
```

At runtime the entire tree is copied into the sandbox root, then the sandbox marks
every plain file under `mocks/` executable and prepends the resolved absolute path
to the agent subprocess `PATH`. The agent can then invoke the bare command names
(`say_hello`, `echo_args`) and the mock implementations win the lookup ahead of any
real binary on the host.

Each mock composes its output line at runtime from the invocation (argument count
and argument lengths) rather than echoing a fixed signature. The task's criteria
assert on those computed receipts, so the expected text is not present verbatim in
any file that ships into the sandbox and the mocks have to actually be executed.
