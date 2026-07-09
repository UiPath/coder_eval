# Experiment Report: three-way

**Description**: Three-way comparison
**Variants**: a, b, c
**Total Duration**: 240.0s

## Aggregate Metrics

| Metric | a | b | c |
|--------|--------|--------|--------|
| Tasks Run | 2 | 2 | 2 |
| Succeeded | 1 | 1 | 1 |
| Failed | 1 | 1 | 1 |
| Errors | 0 | 0 | 0 |
| Success Rate | 50.0% | 50.0% | 50.0% |
| Score | 0.650 ± 0.354 | 0.775 ± 0.247 | 0.650 ± 0.212 |
| Avg Duration (s) | 21.0 ± 1.4 | 26.5 ± 2.1 | 31.5 ± 2.1 |
| Assistant Turns | 3.5 ± 0.7 | 6.0 ± 1.4 | 5.5 ± 0.7 |
| Tokens | 925 ± 35 | 1,125 ± 35 | 1,325 ± 35 |

## Win Rates

- **a**: 1/2 tasks (50%)
- **b**: 1/2 tasks (50%)
- **c**: 0/2 tasks (0%)

## Per-Task Comparison

| Task | a | b | c | Best | Spread |
|------|------|------|------|------|--------|
| task-1 | 0.900 (+) | 0.600 (-) | 0.800 (-) | a | 0.300 |
| task-2 | 0.400 (-) | 0.950 (+) | 0.500 (-) | b | 0.550 |

## Most Divergent Tasks

- **task-2**: spread=0.550, best=b
- **task-1**: spread=0.300, best=a
