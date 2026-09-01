#!/usr/bin/env python3
"""Report every module that touches a Windows-only name at *import* scope.

Run from the repo root, after every rebase onto a new upstream tag:

    python3 tools/scan_module_level_win32.py            # report
    python3 tools/scan_module_level_win32.py --check    # fail if the picture changed

It deliberately skips function/method/class bodies (lazy, so harmless on Linux until
called), upstream's own `if sys.platform == ...` / `os.name` guards (those branches never
run here), and `try:` blocks (the author already tolerates failure). What it flags is what
`ok/compat/win32_stub.py` has to cover.

The CALLED-AT-IMPORT column is the one to watch: a module that *calls* a DLL loader while
importing breaks any stub whose `__call__` raises unconditionally. If a new name appears
there, revisit the stub before anything else.
"""

import sys

# Baseline for ok-script 2.0.5. `--check` fails if either drifts.
EXPECTED_TOTAL = 27
EXPECTED_CALLED_AT_IMPORT = {
    'ok/rotypes/Windows/Foundation/__init__.py',
    'ok/rotypes/roapi.py',
    'ok/rotypes/winstring.py',
    'ok/util/window.py',
}

import ast, os

WIN_MODS = {'win32api','win32con','win32gui','win32process','win32ui','win32clipboard',
            'win32file','pydirectinput','pycaw','comtypes','pythoncom','winreg','d3dshot'}
CTYPES_MISSING = {'windll','WinDLL','oledll','OleDLL','HRESULT','WINFUNCTYPE'}
PLATFORM_SRC = ('sys.platform', 'os.name', 'platform.system')
LOADERS = ('windll', 'oledll', 'WinDLL', 'OleDLL')


def is_platform_guard(test):
    return any(s in ast.unparse(test) for s in PLATFORM_SRC)


def scan(body, hits, called):
    for node in body:
        # function/method/class bodies are lazy -> harmless on Linux until called
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # upstream's own `if sys.platform == 'win32':` blocks never execute on Linux
        if isinstance(node, ast.If) and is_platform_guard(node.test):
            continue
        if isinstance(node, ast.Try):          # already tolerated by the author
            continue
        if isinstance(node, ast.Import):
            hits |= {a.name for a in node.names if a.name.split('.')[0] in WIN_MODS}
        elif isinstance(node, ast.ImportFrom):
            if (node.module or '').split('.')[0] in WIN_MODS:
                hits.add(node.module)
            if node.module == 'ctypes':
                hits |= {'ctypes.' + a.name for a in node.names if a.name in CTYPES_MISSING}
        for x in ast.walk(node):
            if isinstance(x, ast.Call):
                f = ast.unparse(x.func)
                if any(f.startswith(p) or ('.' + p) in f for p in LOADERS):
                    called.add(f)              # <- these break a naive stub [V21]
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) \
                    and x.value.id == 'ctypes' and x.attr in CTYPES_MISSING:
                hits.add('ctypes.' + x.attr)
            elif isinstance(x, ast.Name) and x.id in CTYPES_MISSING \
                    and not isinstance(getattr(x, 'ctx', None), ast.Store):
                hits.add(x.id)                 # Store excludes `HRESULT = LONG`


def main():
    check = '--check' in sys.argv[1:]
    total = 0
    loaders_at_import = set()
    for root, _, files in os.walk('ok'):
        for f in sorted(files):
            if not f.endswith('.py'):
                continue
            p = os.path.join(root, f).replace(os.sep, '/')
            hits, called = set(), set()
            scan(ast.parse(open(p, encoding='utf-8').read()).body, hits, called)
            if hits or called:
                total += 1
                if called:
                    loaders_at_import.add(p)
                extra = f'   CALLED-AT-IMPORT:{sorted(called)}' if called else ''
                print(f'{p:60s} {" ".join(sorted(hits))}{extra}')
    print('TOTAL', total)

    if not check:
        return 0

    problems = []
    if total != EXPECTED_TOTAL:
        problems.append(f'offender count moved {EXPECTED_TOTAL} -> {total}')
    new = loaders_at_import - EXPECTED_CALLED_AT_IMPORT
    gone = EXPECTED_CALLED_AT_IMPORT - loaders_at_import
    if new:
        problems.append(f'NEW module(s) call a DLL loader at import: {sorted(new)} '
                        f'-- re-check ok/compat/win32_stub.py before anything else')
    if gone:
        problems.append(f'no longer call a loader at import: {sorted(gone)} '
                        f'-- update EXPECTED_CALLED_AT_IMPORT')
    for line in problems:
        print('DRIFT:', line)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
