"""Code similarity scoring using AST and token analysis.

This module provides the SimilarityScorer class for evaluating structural
similarity between agent-generated code and reference implementations.
"""

from .ast_similarity import score_ast_similarity
from .signature_similarity import score_function_signatures
from .token_similarity import score_token_similarity


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
        return score_ast_similarity(agent_code, reference_code)

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
        return score_token_similarity(agent_code, reference_code)

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
        return score_function_signatures(agent_code, reference_code)

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
