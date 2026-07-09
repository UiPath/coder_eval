"""Code complexity scoring using radon metrics.

This module provides the ComplexityScorer class for evaluating cyclomatic
complexity, lines of code, and function count metrics.
"""

from radon.complexity import cc_visit
from radon.raw import analyze


class ComplexityScorer:
    """Calculate code complexity metrics using radon.

    Measures:
    - Cyclomatic complexity (control flow branches)
    - Lines of code (total, source, comments, blank)
    - Function count

    Scores agent code relative to reference baseline, where simpler code
    gets higher scores (within reason - 1.5x reference is threshold).
    """

    def calculate_metrics(self, code: str) -> dict[str, int]:
        """Calculate all complexity metrics for code.

        Args:
            code: Python source code

        Returns:
            Dictionary with metrics:
            {
                'cyclomatic_complexity': int,
                'lines_of_code': int,
                'source_lines': int,
                'comment_lines': int,
                'blank_lines': int,
                'function_count': int
            }
        """
        # Cyclomatic complexity per function
        cc_results = cc_visit(code)
        cyclomatic = sum(r.complexity for r in cc_results)

        # Raw metrics (LOC, comments, etc.)
        raw = analyze(code)

        return {
            "cyclomatic_complexity": cyclomatic,
            "lines_of_code": raw.loc,
            "source_lines": raw.sloc,
            "comment_lines": raw.comments,
            "blank_lines": raw.blank,
            "function_count": len(cc_results),
        }

    def score_complexity(
        self, agent_code: str, reference_code: str, reference_baseline: dict[str, int]
    ) -> dict[str, dict[str, int] | dict[str, float]]:
        """Score agent complexity relative to reference.

        Uses formula: score = 1.0 - min(1.0, agent_metric / (ref_metric * 1.5))

        This means:
        - Agent simpler than reference → score closer to 1.0
        - Agent 50% more complex → score = 0.5
        - Agent 2x+ more complex → score = 0.0

        Args:
            agent_code: Agent's implementation source code
            reference_code: Reference implementation (unused, for signature consistency)
            reference_baseline: Expected metrics from reference-metadata.json
                {
                    'cyclomatic': int,
                    'lines_of_code': int,
                    'function_count': int
                }

        Returns:
            Dictionary with agent metrics and scores:
            {
                'agent_metrics': {...},
                'reference_baseline': {...},
                'scores': {
                    'cyclomatic_score': float,
                    'loc_score': float,
                    'function_score': float,
                    'overall_complexity': float
                }
            }
        """
        agent_metrics = self.calculate_metrics(agent_code)

        scores = {}

        # Cyclomatic complexity score
        ref_cc = max(reference_baseline.get("cyclomatic", 10), 1)
        agent_cc = agent_metrics["cyclomatic_complexity"]
        scores["cyclomatic_score"] = max(0.0, 1.0 - min(1.0, agent_cc / (ref_cc * 1.5)))

        # LOC score
        ref_loc = max(reference_baseline.get("lines_of_code", 50), 1)
        agent_loc = agent_metrics["lines_of_code"]
        scores["loc_score"] = max(0.0, 1.0 - min(1.0, agent_loc / (ref_loc * 1.5)))

        # Function count score (penalize deviation in either direction)
        ref_funcs = reference_baseline.get("function_count", 3)
        agent_funcs = agent_metrics["function_count"]
        scores["function_score"] = max(0.0, 1.0 - abs(agent_funcs - ref_funcs) / max(ref_funcs, 1))

        # Weighted average: complexity (50%), LOC (30%), function count (20%)
        scores["overall_complexity"] = (
            0.5 * scores["cyclomatic_score"] + 0.3 * scores["loc_score"] + 0.2 * scores["function_score"]
        )

        return {"agent_metrics": agent_metrics, "reference_baseline": reference_baseline, "scores": scores}
