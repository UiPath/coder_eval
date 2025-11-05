"""Function signature-based code similarity scoring."""

import ast


def score_function_signatures(agent_code: str, reference_code: str) -> float:
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
    agent_sigs = _extract_signatures(agent_code)
    ref_sigs = _extract_signatures(reference_code)

    # Jaccard similarity: |intersection| / |union|
    if not agent_sigs and not ref_sigs:
        return 1.0  # Both have no functions

    common = len(agent_sigs & ref_sigs)
    total = len(agent_sigs | ref_sigs)
    return common / total if total > 0 else 0.0


def _extract_signatures(code: str) -> set[str]:
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
