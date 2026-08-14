# Experiment Report: rep-test

**Description**: Replicate comparison
**Variants**: a, b
**Total Duration**: 60.0s

## Aggregate Metrics

| Metric | a | b | p-value |
|--------|--------|--------|--------|
| Tasks Run | 1 | 1 | — |
| Succeeded | 1 | 1 | — |
| Failed | 0 | 0 | — |
| Errors | 0 | 0 | — |
| Pass Rate | 100.0% | 100.0% | — |
| Score | 0.900 | 0.650 | — |
| Avg Duration (s) | 3.3 | 3.3 | — |
| Tokens | 1,000 | 1,000 | — |
| Replicates/task | 3 | 3 | — |

## Win Rates

- **a**: 1/1 tasks (100%)
- **b**: 0/1 tasks (0%)

## Per-Task Comparison

| Task | a | b | Best | Spread | Reps |
|------|------|------|------|--------|------|
| task-1 | 0.900 (+) | 0.650 (+) | a | 0.250 | 3 |

## Most Divergent Tasks

- **task-1**: spread=0.250, best=a

## Replicate Statistics

| Variant | Replicates/task | Mean score | 95% CI | Pass-rate (Wilson 95%) |
|---------|-----------------|------------|--------|------------------------|
| a | 3 | 0.900 | [0.850, 0.950] | 2/3 [0.21, 0.94] |
| b | 3 | 0.650 | [0.600, 0.700] | 0/3 [0.00, 0.56] |

## Paired Comparison

*A paired comparison needs at least 2 tasks common to a and b; found 1.*
