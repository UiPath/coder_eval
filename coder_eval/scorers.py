"""Code scoring utilities for reference implementation comparison.

This module provides three scorer classes for evaluating agent-generated code
against reference implementations using static analysis:

- SimilarityScorer: AST, token, and signature similarity
- ComplexityScorer: Cyclomatic complexity, LOC, function count
- QualityScorer: Type hints, docstrings, error handling

All scorers produce scores in the range [0.0, 1.0] where 1.0 is best.
"""

import ast
import tokenize
from difflib import SequenceMatcher
from io import BytesIO

from radon.complexity import cc_visit
from radon.raw import analyze


class SimilarityScorer:
    """Calculate structural similarity between code files.

    Compares code structure using:
    - AST node types and tree structure
    - Token sequences (types, not values)
    - Function signatures (names, parameters, return types)

    All scores are in range [0.0, 1.0] where 1.0 means identical structure.
    """

    def score_ast_similarity(self, agent_code: str, reference_code: str) -> float:
        """Compare AST structures using node type sequences.

        Args:
            agent_code: Agent's implementation source code
            reference_code: Reference implementation source code

        Returns:
            Similarity score [0.0, 1.0] based on node type sequence matching

        Raises:
            SyntaxError: If either code cannot be parsed
        """
        agent_ast = ast.parse(agent_code)
        ref_ast = ast.parse(reference_code)

        agent_nodes = self._get_node_types(agent_ast)
        ref_nodes = self._get_node_types(ref_ast)

        return SequenceMatcher(None, agent_nodes, ref_nodes).ratio()

    def _get_node_types(self, tree: ast.AST) -> list[str]:
        """Extract node types from AST in depth-first order.

        Args:
            tree: Parsed AST

        Returns:
            List of node type names (e.g., ['Module', 'FunctionDef', 'Return'])
        """
        return [type(node).__name__ for node in ast.walk(tree)]

    def score_token_similarity(self, agent_code: str, reference_code: str) -> float:
        """Compare tokenized code sequences.

        Uses token types (not literal values) for flexibility. For example,
        different variable names or string literals won't affect the score.

        Args:
            agent_code: Agent's implementation source code
            reference_code: Reference implementation source code

        Returns:
            Similarity score [0.0, 1.0] based on token type sequence matching
        """
        agent_tokens = self._tokenize(agent_code)
        ref_tokens = self._tokenize(reference_code)

        return SequenceMatcher(None, agent_tokens, ref_tokens).ratio()

    def _tokenize(self, code: str) -> list[str]:
        """Extract token types from code.

        Args:
            code: Python source code

        Returns:
            List of token type names (e.g., ['NAME', 'OP', 'NUMBER'])
        """
        tokens = []
        try:
            readline = BytesIO(code.encode()).readline
            for tok in tokenize.tokenize(readline):
                tokens.append(tokenize.tok_name[tok.type])
        except tokenize.TokenError:
            # Incomplete code - return what we have
            pass
        return tokens

    def score_function_signatures(self, agent_code: str, reference_code: str) -> float:
        """Compare function signatures using Jaccard similarity.

        Extracts function signatures in format: 'name(arg1, arg2) -> return_type'

        Args:
            agent_code: Agent's implementation source code
            reference_code: Reference implementation source code

        Returns:
            Jaccard similarity [0.0, 1.0] of signature sets

        Raises:
            SyntaxError: If either code cannot be parsed
        """
        agent_sigs = self._extract_signatures(agent_code)
        ref_sigs = self._extract_signatures(reference_code)

        # Jaccard similarity: |intersection| / |union|
        if not agent_sigs and not ref_sigs:
            return 1.0  # Both have no functions

        common = len(agent_sigs & ref_sigs)
        total = len(agent_sigs | ref_sigs)
        return common / total if total > 0 else 0.0

    def _extract_signatures(self, code: str) -> set[str]:
        """Extract function signatures from code.

        Args:
            code: Python source code

        Returns:
            Set of signatures like 'main(input: Input) -> Output'
        """
        tree = ast.parse(code)
        signatures = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                # Get argument names
                args = [arg.arg for arg in node.args.args]

                # Get return type annotation
                ret = ast.unparse(node.returns) if node.returns else "None"

                # Format: name(arg1, arg2) -> return_type
                sig = f"{node.name}({', '.join(args)}) -> {ret}"
                signatures.add(sig)

        return signatures

    def calculate_similarity(self, agent_code: str, reference_code: str) -> dict[str, float]:
        """Calculate overall similarity score with detailed breakdown.

        Combines three similarity metrics:
        - AST similarity (40% weight): Structural similarity
        - Token similarity (30% weight): Implementation similarity
        - Signature similarity (30% weight): API similarity

        Args:
            agent_code: Agent's implementation source code
            reference_code: Reference implementation source code

        Returns:
            Dictionary with individual scores and overall weighted average:
            {
                'ast_similarity': float,
                'token_similarity': float,
                'signature_similarity': float,
                'overall_similarity': float
            }

        Raises:
            SyntaxError: If either code cannot be parsed
        """
        ast_sim = self.score_ast_similarity(agent_code, reference_code)
        token_sim = self.score_token_similarity(agent_code, reference_code)
        sig_sim = self.score_function_signatures(agent_code, reference_code)

        return {
            "ast_similarity": ast_sim,
            "token_similarity": token_sim,
            "signature_similarity": sig_sim,
            "overall_similarity": (0.4 * ast_sim + 0.3 * token_sim + 0.3 * sig_sim),
        }


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
        ref_cc = reference_baseline.get("cyclomatic", 10)
        agent_cc = agent_metrics["cyclomatic_complexity"]
        scores["cyclomatic_score"] = max(0.0, 1.0 - min(1.0, agent_cc / (ref_cc * 1.5)))

        # LOC score
        ref_loc = reference_baseline.get("lines_of_code", 50)
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


class QualityScorer:
    """Score code quality using AST analysis.

    Evaluates:
    - Type hints: Parameter and return type annotations
    - Docstrings: Module and function documentation
    - Error handling: Try/except blocks

    All scores in range [0.0, 1.0] where 1.0 is highest quality.
    """

    def score_type_hints(self, code: str) -> float:
        """Check type annotation coverage.

        Scores each function based on:
        - Return type annotation (50% weight)
        - Parameter type annotations (50% weight)

        Args:
            code: Python source code

        Returns:
            Type hint coverage score [0.0, 1.0]

        Raises:
            SyntaxError: If code cannot be parsed
        """
        tree = ast.parse(code)
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]

        if not functions:
            return 1.0  # No functions to annotate

        total_score = 0.0
        for func in functions:
            # Return type (50% weight)
            has_return = func.returns is not None

            # Parameter types (50% weight)
            total_params = len(func.args.args)
            if total_params > 0:
                params_annotated = sum(1 for arg in func.args.args if arg.annotation)
                param_coverage = params_annotated / total_params
            else:
                param_coverage = 1.0  # No params to annotate

            func_score = 0.5 * (1.0 if has_return else 0.0) + 0.5 * param_coverage
            total_score += func_score

        return total_score / len(functions)

    def score_docstrings(self, code: str) -> float:
        """Check docstring coverage.

        Scores based on:
        - Module docstring (30% weight)
        - Function docstrings (70% weight)

        Args:
            code: Python source code

        Returns:
            Docstring coverage score [0.0, 1.0]

        Raises:
            SyntaxError: If code cannot be parsed
        """
        tree = ast.parse(code)

        # Module docstring
        module_doc = ast.get_docstring(tree) is not None

        # Function docstrings
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        if not functions:
            return 1.0 if module_doc else 0.5

        documented = sum(1 for func in functions if ast.get_docstring(func))

        # 30% module doc, 70% function docs
        return 0.3 * (1.0 if module_doc else 0.0) + 0.7 * (documented / len(functions))

    def score_error_handling(self, code: str) -> float:
        """Check error handling presence.

        Looks for try/except blocks relative to function count.
        More try blocks (up to 1:1 ratio with functions) = better score.

        Args:
            code: Python source code

        Returns:
            Error handling score [0.0, 1.0]

        Raises:
            SyntaxError: If code cannot be parsed
        """
        tree = ast.parse(code)

        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        try_blocks = [n for n in ast.walk(tree) if isinstance(n, ast.Try)]

        if not functions:
            return 0.5  # Neutral score for no functions

        if not try_blocks:
            return 0.3  # Partial credit (some tasks don't need error handling)

        # More try blocks = better (up to 1.0)
        return min(1.0, 0.5 + 0.5 * (len(try_blocks) / len(functions)))

    def calculate_quality(self, code: str) -> dict[str, float]:
        """Calculate overall quality score with detailed breakdown.

        Combines three quality metrics:
        - Type hints (30% weight)
        - Docstrings (30% weight)
        - Error handling (40% weight)

        Args:
            code: Python source code

        Returns:
            Dictionary with individual scores and overall weighted average:
            {
                'type_hints': float,
                'docstrings': float,
                'error_handling': float,
                'overall_quality': float
            }

        Raises:
            SyntaxError: If code cannot be parsed
        """
        type_hints = self.score_type_hints(code)
        docstrings = self.score_docstrings(code)
        error_handling = self.score_error_handling(code)

        return {
            "type_hints": type_hints,
            "docstrings": docstrings,
            "error_handling": error_handling,
            "overall_quality": (0.3 * type_hints + 0.3 * docstrings + 0.4 * error_handling),
        }
