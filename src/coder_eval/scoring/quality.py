"""Code quality scoring using AST analysis.

This module provides the QualityScorer class for evaluating type hints,
docstrings, and error handling coverage.
"""

import ast


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
        functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

        if not functions:
            return 1.0  # No functions to annotate

        total_score = 0.0
        for func in functions:
            # Return type (50% weight)
            has_return = func.returns is not None

            # Parameter types (50% weight) - exclude self/cls which don't need annotations
            params = [arg for arg in func.args.args if arg.arg not in ("self", "cls")]
            total_params = len(params)
            if total_params > 0:
                params_annotated = sum(1 for arg in params if arg.annotation)
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
        functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
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

        functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
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
