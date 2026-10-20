#!/usr/bin/env bash
# Runs only the GUI tests (package tls.ui), same options as run-matrix.sh. To run all suites on one boot use
#   SUITES=all ./run-matrix.sh
SUITES=gui exec "$(dirname "$0")/run-matrix.sh" "$@"
