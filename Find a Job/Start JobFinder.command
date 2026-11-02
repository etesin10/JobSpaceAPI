#!/usr/bin/env bash
# Double-click in Finder. First time: right-click > Open (macOS asks once for downloaded scripts).
cd "$(dirname "$0")" && exec bash ./scripts/bootstrap.sh start
