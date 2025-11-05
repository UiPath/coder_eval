"""Token-based code similarity scoring."""

import tokenize
from difflib import SequenceMatcher
from io import BytesIO


def score_token_similarity(agent_code: str, reference_code: str) -> float:
    """Compare tokenized code sequences.

    Uses token types (not literal values) for flexibility. For example,
    different variable names or string literals won't affect the score.

    Args:
        agent_code: Agent's implementation source code
        reference_code: Reference implementation source code

    Returns:
        Similarity score [0.0, 1.0] based on token type sequence matching
    """
    agent_tokens = _tokenize(agent_code)
    ref_tokens = _tokenize(reference_code)

    return SequenceMatcher(None, agent_tokens, ref_tokens).ratio()


def _tokenize(code: str) -> list[str]:
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
