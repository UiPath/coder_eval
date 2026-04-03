#!/bin/bash
# Deployment script for my UiPath agent
# BUG: --format json is only valid for native uip commands (login, setup),
# NOT for forwarded Python CLI commands (run, deploy, init, eval)

set -e

echo "Step 1: Initialize project"
uip codedagents init --format json

echo "Step 2: Run smoke test"
uip codedagents run word_count '{"text": "hello world"}' --format json

echo "Step 3: Deploy to Orchestrator"
uip codedagents deploy --my-workspace --format json

echo "Done!"
