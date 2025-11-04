# Python Starter Template

This is a template directory for creating Python projects with coder_eval.

## Structure

```
python-starter/
├── README.md          # This file
├── src/               # Source code directory
│   └── main.py        # Main module with stub function
└── tests/             # Test directory
    └── test_main.py   # Test template
```

## Usage

Reference this template in your task YAML:

```yaml
sandbox:
  driver: tempdir
  python_version: "3.12"
  template_dir: "./templates/python-starter"
```

The agent will start with these files already in the sandbox.
