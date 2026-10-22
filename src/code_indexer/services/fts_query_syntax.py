"""Full-text query syntax handling shared by the Tantivy index manager.

Moved verbatim from tantivy_index_manager (which re-exports every name):
sanitization of a caller's query before Tantivy's parser sees it, and the
boolean-operator validity rules the query builder routes on.
"""

from typing import List

_BOOL_OPS: frozenset = frozenset({"OR", "AND", "NOT"})


def _drop_orphaned_bool_ops(tokens: List[str]) -> List[str]:
    """Remove OR/AND/NOT tokens that are leading, trailing or doubled.

    'foo OR OR bar' -> 'foo OR bar' (the first of a run is kept),
    'AND foo' -> 'foo', 'foo OR' -> 'foo'.
    """
    result: List[str] = []
    for tok in tokens:
        if tok in _BOOL_OPS and (not result or result[-1] in _BOOL_OPS):
            continue
        result.append(tok)
    if result and result[-1] in _BOOL_OPS:
        # The loop never keeps two operators in a row, so at most one trails.
        result.pop()
    return result


def sanitize_fts_query(query_text: str) -> str:
    """Sanitize an FTS query to prevent Tantivy parse errors.

    Tantivy's parse_query() raises errors for:
      1. Unmatched double-quotes (odd count)
      2. Syntax characters used in code identifiers (colon, parentheses, brackets, braces)
      3. Bare boolean operators with no valid operands (e.g. bare "OR", trailing "term OR",
         leading "OR term", adjacent "term OR AND other")

    Algorithm:
      Phase 1 - Unmatched quotes:
        - 0 quotes: skip (fast path)
        - even count: unchanged (valid phrase search like "hello world")
        - odd count: strip all " characters

      Phase 2 - Escape Tantivy syntax characters:
        Characters that cause parse errors in code identifiers are escaped/removed.
        Safe characters (., ~, *, ^, +, -) are preserved (useful for wildcards, fuzzy, etc.).
        - Colon (:): replaced with space — prevents "Field does not exist" ValueError
          (Tantivy interprets field:value syntax)
        - Parentheses (), brackets [], braces {}: stripped — prevent "Syntax Error" or
          "Unsupported query" ValueError (Tantivy grouping/range syntax)

      Phase 2b - '+'/'-' operators:
        - Tokens consisting only of '+'/'-' are dropped ("Syntax Error: -").
        - When a token was dropped, OR/AND/NOT operators it left leading,
          trailing or doubled are removed ('foo OR -' -> 'foo',
          'foo OR - OR bar' -> 'foo OR bar').
        - If the query then has no valid boolean operator, a leading '+'/'-'
          run is stripped, so '-bar' / '--verbose' search the literal word
          (per-term parsing rejects '-bar' alone). A valid boolean query keeps
          its prefixes: parse_query() handles 'foo AND -bar' itself.
        - Inner and trailing operators ('foo-bar', 'foo+') are kept.

      Phase 3 - Bare boolean operators:
        Runs AFTER Phase 2 so boolean validation sees the final token structure
        after all character escaping. This ensures that e.g. 'ns:OR' (one token)
        first becomes 'ns OR' (via Phase 2), then 'ns or' (via Phase 3, trailing OR).
        Only all-uppercase "OR", "AND", "NOT" are Tantivy operators.
        Mixed-case variants ("Or", "aNd") are treated as literals — unchanged.
        - OR and AND require a non-operator term on BOTH left and right sides.
          If either side is missing or is another operator, lowercase the token.
        - NOT requires only a non-operator term on the RIGHT side.
          If the right side is missing or is another operator, lowercase the token.

    Bug #353: By preventing the parse error at source, error bursts (e.g.
    ~120 repeated errors across repositories) are eliminated implicitly.
    Bug #357: Phase 2 must run before Phase 3. When a colon-separated identifier
    like 'ns:OR' is split by the colon, the resulting tokens must be evaluated
    for boolean validity rather than left as an unsafe trailing operator.
    """
    # Phase 1: handle unmatched quotes
    quote_count = query_text.count('"')
    if quote_count != 0 and quote_count % 2 != 0:
        query_text = query_text.replace('"', "")

    # Phase 2: escape Tantivy query syntax characters that appear in code identifiers.
    # This must run BEFORE Phase 3 (boolean validation) so that colon-separated tokens
    # like 'ns:OR' are split into ['ns', 'OR'] before boolean validity is checked.
    # Colon: causes "Field does not exist" ValueError (Tantivy field:value syntax)
    query_text = query_text.replace(":", " ")
    # Parentheses, brackets, braces: cause "Syntax Error" or "Unsupported query" ValueError
    for ch in "()[]{}":
        query_text = query_text.replace(ch, "")

    # Phase 2b: '+'/'-' operators. A token made only of '+'/'-' is always
    # dropped ("Syntax Error: -"), together with any boolean operator that
    # removal orphans ('foo OR -'), rather than searching it as a literal word.
    # A valid boolean query goes whole to parse_query(), which handles
    # '-term'/'+term' itself, so prefixes are kept there. Every other query is
    # split into per-term queries where '-bar' alone fails ("Only excluding
    # terms given"), so a leading run is stripped and the literal word searched.
    # Inner and trailing operators ('foo-bar', 'foo+') parse fine and are kept.
    tokens = query_text.split()
    if any(tok[0] in "+-" for tok in tokens):
        kept = [tok for tok in tokens if tok.strip("+-")]
        if len(kept) < len(tokens):
            kept = _drop_orphaned_bool_ops(kept)
        if not _contains_valid_boolean_ops(" ".join(kept)):
            # '-OR' strips to the literal word 'or', never a live operator:
            # the query was classified as non-boolean above.
            stripped = (tok.lstrip("+-") for tok in kept)
            kept = [tok.lower() if tok in _BOOL_OPS else tok for tok in stripped]
        query_text = " ".join(kept)

    # Phase 3: handle bare boolean operators (runs after syntax escaping)
    tokens = query_text.split()
    if any(tok in _BOOL_OPS for tok in tokens):
        result = list(tokens)
        for i, tok in enumerate(tokens):
            if tok not in _BOOL_OPS:
                continue

            # Check immediate neighbors (not skipping over other operators).
            # An operator immediately adjacent to another operator means invalid usage.
            immediate_left = tokens[i - 1] if i > 0 else None
            immediate_right = tokens[i + 1] if i < len(tokens) - 1 else None

            # A valid left operand: exists AND is not itself a boolean operator
            has_left = immediate_left is not None and immediate_left not in _BOOL_OPS
            # A valid right operand: exists AND is not itself a boolean operator
            has_right = immediate_right is not None and immediate_right not in _BOOL_OPS

            # Determine validity:
            # NOT is valid when it has a non-operator term on the right.
            # OR and AND are valid when they have non-operator terms on BOTH sides.
            if tok == "NOT":
                is_valid = has_right
            else:
                is_valid = has_left and has_right

            if not is_valid:
                result[i] = tok.lower()

        query_text = " ".join(result) if tokens else query_text

    return query_text


def _contains_valid_boolean_ops(query_text: str) -> bool:
    """Detect if a sanitized query contains valid boolean operators.

    Returns True if the query contains OR/AND/NOT operators with proper operands,
    meaning it should be passed directly to Tantivy's parse_query() instead of
    being split into per-term queries.

    Uses the same validation rules as sanitize_fts_query():
    - OR/AND need non-operator terms on BOTH left and right sides
    - NOT needs non-operator terms on BOTH sides (bare 'NOT term' rejected
      by Tantivy: 'Only excluding terms given')
    - Only all-uppercase OR, AND, NOT are Tantivy operators
    """
    tokens = query_text.split()
    if len(tokens) < 2:
        return False

    for i, token in enumerate(tokens):
        if token not in _BOOL_OPS:
            continue

        left = tokens[i - 1] if i > 0 else None
        right = tokens[i + 1] if i < len(tokens) - 1 else None
        has_left = left is not None and left not in _BOOL_OPS
        has_right = right is not None and right not in _BOOL_OPS

        # All operators (OR, AND, NOT) require non-operator terms on both sides.
        # OR/AND: standard boolean grammar requires operands on both sides.
        # NOT: Tantivy rejects bare 'NOT term' ('Only excluding terms given'),
        #      so only compound 'positive_term NOT excluded_term' is valid.
        if has_left and has_right:
            return True

    return False
