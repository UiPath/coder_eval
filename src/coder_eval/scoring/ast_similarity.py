"""AST-based code similarity scoring."""

import ast
from difflib import SequenceMatcher


def score_ast_similarity(agent_code: str, reference_code: str) -> float:
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

    agent_nodes = _get_node_types(agent_ast)
    ref_nodes = _get_node_types(ref_ast)

    return SequenceMatcher(None, agent_nodes, ref_nodes).ratio()


def _get_node_types(tree: ast.AST) -> list[str]:
    """Extract node types from AST in depth-first order.

    Args:
        tree: Parsed AST

    Returns:
        List of node type names (e.g., ['Module', 'FunctionDef', 'Return'])
    """
    return [type(node).__name__ for node in ast.walk(tree)]
