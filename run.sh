#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
mkdir -p data
exec python3 app.py
