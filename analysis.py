import ast
import io
import re
import tokenize
from difflib import SequenceMatcher

def analyze_code_quality(code):
    if not code or not code.strip():
        return {
            'naming': 0, 'structure': 0, 'comments': 0,
            'complexity': 0, 'conciseness': 0, 'type_hints': 0,
            'total_lines': 0
        }

    lines = code.split('\n')
    total_lines = len(lines)
    non_empty = [l for l in lines if l.strip()]

    naming_score = _analyze_naming(code)
    structure_score = _analyze_structure(code)
    comments_score = _analyze_comments(code, lines, total_lines)
    complexity_score = _analyze_complexity(code)
    conciseness_score = _analyze_conciseness(code, lines, total_lines)
    type_hints_score = _analyze_type_hints(code)

    return {
        'naming': naming_score,
        'structure': structure_score,
        'comments': comments_score,
        'complexity': complexity_score,
        'conciseness': conciseness_score,
        'type_hints': type_hints_score,
        'total_lines': total_lines
    }

def _analyze_naming(code):
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return 20

    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.FunctionDef):
            names.append(node.name)
        elif isinstance(node, ast.arg):
            names.append(node.arg)

    if not names:
        return 30

    total = 0
    for name in names:
        if name.startswith('_') or len(name) == 1:
            total += 0.1
        elif '_' in name and name == name.lower():
            total += 0.5 + min(0.5, len(name) * 0.05)
        elif name[0].islower() and '_' not in name and any(c.isupper() for c in name[1:]):
            total += 0.4 + min(0.3, len(name) * 0.04)
        elif name[0].isupper():
            total += 0.3
        elif len(name) >= 3:
            total += 0.2 + min(0.3, len(name) * 0.03)
        elif len(name) == 2:
            total += 0.1

    raw = min(100, int(total / max(1, len(names)) * 100))
    return max(5, raw)

def _analyze_structure(code):
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return 15

    lines = code.split('\n')
    total_lines = len([l for l in lines if l.strip()])

    func_count = 0
    class_count = 0
    has_docstring = False
    total_func_lines = 0
    max_depth = 0
    has_import = False
    blank_count = 0

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            func_count += 1
            if ast.get_docstring(node):
                has_docstring = True
            end = node.end_lineno or total_lines
            start = node.lineno
            total_func_lines += end - start + 1
            depth = _count_depth(node)
            if depth > max_depth:
                max_depth = depth
        elif isinstance(node, ast.ClassDef):
            class_count += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            has_import = True


    for line in lines:
        if line.strip() == '':
            blank_count += 1

    score = 25.0
    if func_count > 0:
        score += min(25, func_count * 7)
    if class_count > 0:
        score += min(15, class_count * 7)
    if has_docstring:
        score += 12
    if has_import:
        score += 5
    if max_depth > 1:
        score += min(12, max_depth * 3)

    if total_lines > 10:
        blank_ratio = blank_count / total_lines
        if 0.05 <= blank_ratio <= 0.25:
            score += 6
        elif blank_ratio > 0.25:
            score -= 4
        elif blank_ratio < 0.02:
            score -= 3

    if total_lines > 0 and total_func_lines > 0:
        func_ratio = total_func_lines / total_lines
        if 0.4 <= func_ratio <= 0.9:
            score += 6
        elif func_ratio > 0.95:
            score -= 2

    if total_lines >= 30:
        score += 4
    elif total_lines >= 15:
        score += 2
    elif total_lines < 5:
        score -= 5

    comment_count = sum(1 for l in lines if l.strip().startswith('#') or '  #' in l or ' #' in l)
    if comment_count >= 3:
        score += 4
    elif comment_count >= 1:
        score += 2

    return max(10, min(100, int(score)))

def _count_depth(node, current=0):
    max_d = current
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.If, ast.For, ast.While, ast.Try, ast.With)):
            d = _count_depth(child, current + 1)
            if d > max_d:
                max_d = d
        else:
            d = _count_depth(child, current)
            if d > max_d:
                max_d = d
    return max_d


def _analyze_comments(code, lines, total_lines):
    if total_lines == 0:
        return 0

    comment_lines = 0
    in_docstring = False
    for line in lines:
        stripped = line.strip()


        if stripped.startswith('"""') or stripped.startswith("'''"):
            comment_lines += 1
            quote = stripped[:3]
            if stripped.count(quote) < 2:
                in_docstring = not in_docstring
            continue
        if in_docstring:
            comment_lines += 1
            if stripped.endswith('"""') or stripped.endswith("'''"):
                in_docstring = False
            continue

        if stripped.startswith('#'):
            comment_lines += 1

        elif '  #' in line or ' #' in line:
            comment_lines += 1

    ratio = comment_lines / total_lines

    if ratio <= 0.01:
        return max(1, int(ratio * 500))
    elif ratio <= 0.25:
        return min(100, int(ratio * 400))
    elif ratio <= 0.5:
        return max(30, int(100 - (ratio - 0.25) * 200))
    else:
        return max(10, int(50 - (ratio - 0.5) * 100))


def _analyze_complexity(code):
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return 10

    total_complexity = 0
    func_count = 0
    total_lines = len(code.split('\n'))

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            func_count += 1
            complexity = 1
            for child in ast.walk(node):
                if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler,
                                       ast.And, ast.Or, ast.Try, ast.With)):
                    complexity += 1
                elif isinstance(child, ast.BoolOp):
                    complexity += len(child.values) - 1
            total_complexity += complexity

    if func_count == 0:
        return max(40, min(90, 100 - max(0, total_lines - 5) * 2))

    avg_cc = total_complexity / func_count

    if avg_cc <= 2:
        return max(35, int(100 - (2 - avg_cc) * 20))
    elif avg_cc <= 5:
        return max(35, int(100 - (avg_cc - 2) * 18))
    elif avg_cc <= 10:
        return max(10, int(64 - (avg_cc - 5) * 6))
    else:
        return max(5, int(34 - (avg_cc - 10) * 2))


def _analyze_conciseness(code, lines, total_lines):
    if total_lines == 0:
        return 0
    non_empty = [l for l in lines if l.strip() and not l.strip().startswith('#')]
    if not non_empty:
        return 0

    lengths = [len(l) for l in non_empty]
    avg_len = sum(lengths) / len(lengths)
    long_lines = sum(1 for l in non_empty if len(l) > 100)
    short_lines = sum(1 for l in non_empty if len(l) < 10)
    max_line_len = max(lengths)
    min_line_len = min(lengths)



    optimal_lines = 18
    if total_lines <= optimal_lines:
        line_score = max(10, 55 - (optimal_lines - total_lines) * 2.5)
    else:
        line_score = max(5, 55 - (total_lines - optimal_lines) * 2.0)
    score = line_score


    if avg_len < 18:
        score -= min(25, int((18 - avg_len) * 2))
    elif avg_len > 70:
        score -= min(25, int((avg_len - 70) * 1.5))
    elif 25 <= avg_len <= 55:
        score += 12
    elif 18 <= avg_len < 25:
        score += min(12, int((avg_len - 18) * 1.5))
    elif 55 < avg_len <= 70:
        score += min(12, int((70 - avg_len) * 0.8))


    if len(lengths) > 1:
        mean = sum(lengths) / len(lengths)
        variance = sum((x - mean) ** 2 for x in lengths) / len(lengths)
        std_dev = variance ** 0.5
        if std_dev > 20:
            score += min(10, int(std_dev * 0.3))
        elif std_dev < 4:
            score -= 12  # too uniform — all lines same length
        if std_dev >= 8:
            score += 5


    if long_lines > 0:
        score -= min(25, long_lines * 8)


    if total_lines > 10:
        short_ratio = short_lines / len(non_empty)
        if short_ratio > 0.3:
            score -= min(20, int(short_ratio * 40))


    if max_line_len > 0 and max_line_len - min_line_len > 40:
        score += 8
    elif max_line_len > 0 and max_line_len - min_line_len > 20:
        score += 4

    return max(10, min(100, int(score)))


def _analyze_type_hints(code):
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return 0

    total_args = 0
    hinted_args = 0
    func_count = 0
    funcs_with_return = 0

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            func_count += 1
            if node.args.args:
                for arg in node.args.args:
                    total_args += 1
                    if arg.annotation is not None:
                        hinted_args += 1
            if node.returns is not None:
                funcs_with_return += 1

    if func_count == 0:
        return 30


    arg_score = (hinted_args / max(1, total_args)) * 80 if total_args > 0 else 0
    return_bonus = min(20, (funcs_with_return / func_count) * 20)
    return max(0, min(100, int(arg_score + return_bonus)))

def detect_ai_code(code):
    if not code or not code.strip():
        return False, 0, {}

    lines = code.split('\n')
    total = len(lines)
    non_empty = [l for l in lines if l.strip()]

    comment_lines = sum(1 for l in lines if l.strip().startswith('#'))
    comment_ratio = comment_lines / max(1, total)

    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None

    var_names = []
    func_line_counts = []
    advanced_syntax_count = 0

    if tree:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                var_names.append(node.id)
            elif isinstance(node, ast.FunctionDef):
                end = node.end_lineno or total
                start = node.lineno
                func_line_counts.append(end - start + 1)
            elif isinstance(node, (ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
                advanced_syntax_count += 1
            elif isinstance(node, ast.Lambda):
                advanced_syntax_count += 1
            elif isinstance(node, ast.Try):
                advanced_syntax_count += 1
            elif isinstance(node, ast.With):
                advanced_syntax_count += 1

    avg_var_len = sum(len(n) for n in var_names) / max(1, len(var_names))
    avg_func_lines = sum(func_line_counts) / max(1, len(func_line_counts))

    empty_lines = total - len(non_empty)
    empty_ratio = empty_lines / max(1, total)

    flags = 0
    reasons = {}

    if comment_ratio > 0.15:
        flags += 1
        reasons['comment_ratio'] = f'{comment_ratio:.2f}'

    if avg_var_len > 8:
        flags += 1
        reasons['avg_var_len'] = f'{avg_var_len:.1f}'

    if avg_func_lines < 8 and len(func_line_counts) >= 2:
        flags += 1
        reasons['avg_func_lines'] = f'{avg_func_lines:.1f}'

    if advanced_syntax_count > 0:
        flags += 1
        reasons['advanced_syntax'] = advanced_syntax_count

    if empty_ratio > 0.15:
        flags += 1
        reasons['empty_ratio'] = f'{empty_ratio:.2f}'

    is_ai = flags >= 3
    return is_ai, flags, reasons

def _tokenize(code):
    if not code:
        return ()
    try:
        toks = []
        for tok in tokenize.generate_tokens(io.StringIO(code).readline):
            t = tok.type
            if t in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                     tokenize.ENCODING, tokenize.ENDMARKER,
                     tokenize.INDENT, tokenize.DEDENT):
                continue
            if t == tokenize.STRING:
                toks.append("'...'")
            elif t == tokenize.NUMBER:
                toks.append('0')
            else:
                toks.append(tok.string)
        return tuple(toks)
    except tokenize.TokenError:
        return tuple(code.split())


def _normalize(code):
    if not code:
        return ''
    lines = [l.rstrip() for l in code.split('\n')]
    result = []
    prev_empty = False
    for line in lines:
        empty = not line
        if empty and prev_empty:
            continue
        prev_empty = empty
        result.append(line)
    return '\n'.join(result)


def compute_similarity(code1, code2):
    if not code1 or not code2:
        return 0.0
    sm = SequenceMatcher(None, code1, code2, autojunk=False)
    rqr = sm.real_quick_ratio()
    if rqr < 0.35:
        return rqr
    qr = sm.quick_ratio()
    if qr < 0.35:
        return qr
    return sm.ratio()

def build_similarity_matrix(codes):

    tokenized = []
    for sid, c in codes:
        if isinstance(c, list):
            tokenized.append((sid, [_tokenize(x) for x in c]))
        else:
            tokenized.append((sid, _tokenize(c)))

    n = len(tokenized)
    matrix = []
    for i in range(n):
        row = []
        for j in range(n):
            if i == j:
                row.append(1.0)
            elif j < i:
                row.append(matrix[j][i])
            else:
                ci, cj = tokenized[i][1], tokenized[j][1]
                if isinstance(ci, list) and isinstance(cj, list):
                    q_sims = [compute_similarity(a, b) for a, b in zip(ci, cj)]
                    row.append(sum(q_sims) / max(1, len(q_sims)))
                else:
                    row.append(compute_similarity(ci, cj))
        matrix.append(row)
    return matrix

def generate_diff(code1, code2):
    import difflib
    lines1 = code1.splitlines(keepends=True)
    lines2 = code2.splitlines(keepends=True)
    differ = difflib.Differ()
    diff = list(differ.compare(lines1, lines2))
    return diff
