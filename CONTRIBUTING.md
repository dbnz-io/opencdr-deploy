# Contributing

Create a virtual environment and install the development extra:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ruff check .
pytest
python -m build
python -m twine check dist/*
```

Changes to deployment behavior need unit tests. Commands that can mutate AWS
must provide a dry-run path where practical, must not print credentials, and
must select credentials through the named installation rather than ambient
global state unless the installation explicitly omits an AWS profile.

Do not publish from a workstation. Follow the trusted-publishing release process
in the README.
