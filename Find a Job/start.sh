#!/usr/bin/env bash
# Linux / terminal launcher: ./start.sh [start|update|agents|doctor]
cd "$(dirname "$0")" && exec bash ./scripts/bootstrap.sh "${1:-start}"
