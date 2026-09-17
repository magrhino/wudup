#!/bin/sh
# Generate runtime, build-backend, and CI locks from pyproject.toml.
set -eu
CUSTOM_COMPILE_COMMAND="make lock"
export CUSTOM_COMPILE_COMMAND
pip-compile --strip-extras --generate-hashes pyproject.toml -o requirements.txt
pip-compile --strip-extras --generate-hashes --allow-unsafe \
  --all-build-deps --only-build-deps pyproject.toml -o requirements-build.txt
pip-compile --strip-extras --generate-hashes --allow-unsafe --extra dev \
  -c requirements.txt -c requirements-build.txt pyproject.toml -o requirements-dev.txt
