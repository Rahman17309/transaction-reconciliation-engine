#!/usr/bin/env bash
# Rebuild everything from scratch: data -> cleaning -> SQL engine -> scoring -> 20-month robustness -> notebooks (executed in place).
set -euo pipefail
cd "$(dirname "$0")"
python src/generate_data.py
python src/clean.py
python src/match.py
python src/score.py
python src/robustness.py 20
python src/run_notebooks.py
