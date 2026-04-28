#!/usr/bin/env bash
set -euo pipefail

screen -ls 2>/dev/null | grep -q 'netrun-refill' && screen -S netrun-refill -X quit && echo "stopped netrun-refill" || echo "netrun-refill not running"
screen -ls 2>/dev/null | grep -q 'netrun-validation' && screen -S netrun-validation -X quit && echo "stopped netrun-validation" || echo "netrun-validation not running"
