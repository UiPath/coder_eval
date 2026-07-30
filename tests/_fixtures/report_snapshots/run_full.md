# Evaluation Run Report

**Run ID**: `2025-10-11_12-00-00`
**Date**: 2025-10-11 12:00:00
**Duration**: 330.00s
**Models**: `claude-haiku-4-5`, `claude-sonnet-4-6`

## Summary

- **Total Tasks**: 2
- **Succeeded**: 1
- **Failed**: 1 (incl. 1 token budget, 0 cost budget exceeded)
- **Errors**: 0
- **Pass Rate**: 50.0% (1/2)
- **Avg Reliability Score**: 0.625
- **Avg Generation Latency**: 10.2s
- **Total Assistant Turns**: 4
- **Crashed Partials**: 1 (0 recovered, 1 terminal)
- **Avg Ground Truth Similarity**: 0.715

## Task Details

| Task ID | Status | Reliability Score | Latency | Model | Tags | Similarity | Cmd Efficiency |
|---------|--------|-------------------|---------|-------|------|------------|----------------|
| alpha | success | 0.950 | 12.5s | claude-haiku-4-5 | smoke, fast | 0.880 | 75.0% (4/6) |
| beta | failure | 0.300 | 8.0s | claude-sonnet-4-6 | regression | 0.550 | 100.0% (3/3) |

## Run-time Notes

> **WARNING:** [alpha] max_turns exhausted
> **WARNING:** [beta] expected_turns exceeded: 10/5 (cumulative SDK turns)


## Generation Metrics

| Task ID | Total Latency | Turns | Asst Turns | Avg Turn Latency |
|---------|---------------|-------|------------|------------------|
| alpha | 12.5s | 1 | 3 | 4.2s |
| beta | 8.0s | 1 | 1 | 2.0s |


## Token Usage

**Total Tokens**: 2,300 (input: 1,600, output: 700)
**Cache Tokens**: write: 200, read: 300
**Total Cost**: $0.0200
**Avg Tokens/Task**: 1,150

| Task ID | Input (uncached) | Output | Cache Write | Cache Read | Total | Cost |
|---------|------------------|--------|-------------|------------|-------|------|
| alpha | 1,000 | 500 | 200 | 300 | 1,500 | $0.0123 |
| beta | 600 | 200 | 0 | 0 | 800 | $0.0077 |

## Agent Settings

- **Permission Mode**: N/A
- **Allowed Tools**: (all)
- **Model**: claude-haiku-4-5
- **Max Turns**: 30

## Installed Tools

| Task ID | Tool | Version |
|---------|------|---------|
| alpha | node | 20.1.0 |

## Environment

- **python**: 3.13.11
- **platform**: darwin
