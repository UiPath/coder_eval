# Experiment Report: model-comparison

**Description**: Compare baseline vs mutated
**Variants**: baseline, mutated
**Total Duration**: 180.0s

## Prompt Configuration

- **baseline**: (base prompt)
- **mutated**: (1 mutations: prefix)

## Aggregate Metrics

| Metric | baseline | mutated | p-value |
|--------|--------|--------|--------|
| Tasks Run | 2 | 2 | — |
| Succeeded | 1 | 1 | — |
| Failed | 1 | 1 | — |
| - Token budget | 1 | 0 | — |
| - Cost budget | 0 | 1 | — |
| Errors | 0 | 0 | — |
| Success Rate | 50.0% | 50.0% | — |
| Score | 0.700 ± 0.283 | 0.825 ± 0.177 | 0.658 |
| Avg Duration (s) | 35.0 ± 7.1 | 55.0 ± 7.1 | 0.106 |
| Assistant Turns | 5.5 ± 0.7 | 7.5 ± 0.7 | 0.106 |
| Tokens | 1,100 ± 141 | 1,900 ± 141 | 0.030 |

## Win Rates

- **baseline**: 1/2 tasks (50%)
- **mutated**: 1/2 tasks (50%)

## Per-Task Comparison

| Task | baseline | mutated | Best | Spread |
|------|------|------|------|--------|
| task-a | 0.900 (+) | 0.700 (-) | baseline | 0.200 |
| task-b | 0.500 (-) | 0.950 (+) | mutated | 0.450 |

## Most Divergent Tasks

- **task-b**: spread=0.450, best=mutated
- **task-a**: spread=0.200, best=baseline
