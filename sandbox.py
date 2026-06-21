import ast
import subprocess
import tempfile
import os
import time

FORBIDDEN_MODULES = {
    'os', 'subprocess', 'shutil', 'socket', 'requests', 'sys',
    'ctypes', 'pickle', 'code', 'builtins', 'importlib',
    'signal', 'multiprocessing', 'threading', 'concurrent',
    'pathlib', 'glob', 'fnmatch', 'io', 'tempfile',
}

FORBIDDEN_FUNCS = {
    'exec', 'eval', 'compile', '__import__',
    'globals', 'locals', 'getattr', 'setattr', 'delattr',
    'hasattr', 'vars', 'dir', 'type', 'issubclass',
    'isinstance', 'breakpoint', 'input',
}

ALLOWED_SAFE_IMPORTS = {
    'math', 'cmath', 'decimal', 'fractions', 'random',
    'statistics', 'itertools', 'collections', 'functools',
    'operator', 'string', 're', 'datetime', 'calendar',
    'heapq', 'bisect', 'array', 'copy', 'pprint', 'textwrap',
    'json', 'csv', 'base64', 'hashlib', 'typing',
}

FORBIDDEN_ATTRS = {
    '__class__', '__bases__', '__mro__', '__subclasses__',
    '__globals__', '__code__', '__closure__', '__dict__',
    '__builtins__', '__import__',
}

class CodeSecurityError(Exception):
    pass

class CodeSecurityVisitor(ast.NodeVisitor):
    def __init__(self):
        self.errors = []

    def visit_Import(self, node):
        for alias in node.names:
            mod_name = alias.name.split('.')[0]
            if mod_name in FORBIDDEN_MODULES:
                self.errors.append(f"Forbidden module import: {mod_name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            mod_name = node.module.split('.')[0]
            if mod_name in FORBIDDEN_MODULES:
                self.errors.append(f"Forbidden module import: {mod_name}")
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_FUNCS:
            self.errors.append(f"Forbidden function call: {node.func.id}")
        elif isinstance(node.func, ast.Attribute):
            if node.func.attr in FORBIDDEN_ATTRS:
                self.errors.append(f"Forbidden attribute access: {node.func.attr}")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr in FORBIDDEN_ATTRS:
            self.errors.append(f"Forbidden attribute: {node.attr}")
        self.generic_visit(node)

def scan_code(code):
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"
    visitor = CodeSecurityVisitor()
    visitor.visit(tree)
    if visitor.errors:
        return False, "; ".join(visitor.errors)
    return True, "OK"

def run_code_sandbox(code, test_input='', timeout=5, skip_scan=False):
    if not skip_scan:
        ok, msg = scan_code(code)
        if not ok:
            return False, f"Security check failed: {msg}", ""

    tmpdir = tempfile.mkdtemp(prefix='sandbox_')
    tmpfile = os.path.join(tmpdir, 'user_code.py')
    try:
        with open(tmpfile, 'w', encoding='utf-8') as f:
            f.write(code)

        proc = subprocess.run(
            ['python', tmpfile],
            input=test_input,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=tmpdir,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        if proc.returncode != 0:
            return False, stdout, stderr.strip() or "Runtime error"
        return True, stdout.strip(), stderr.strip()
    except subprocess.TimeoutExpired:
        _cleanup(tmpdir)
        return False, "", "Execution timeout (5s)"
    except Exception as e:
        return False, "", str(e)
    finally:
        _cleanup(tmpdir)

def _cleanup(tmpdir):
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass
