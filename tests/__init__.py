"""Marks `tests` as a package so `from tests.conftest import ...` resolves.

Without this file the suite passes under `python -m pytest`, which puts the
working directory on `sys.path`, and fails under a bare `pytest`, which does not.
CI runs the bare form, so every CI run failed to collect while every local run was
green.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""
