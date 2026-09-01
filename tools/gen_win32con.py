#!/usr/bin/env python3
"""Regenerate `ok/compat/win32con_constants.py` from a real pywin32 `win32con`.

Run after every upstream rebase, and any time the constant sweep below changes count.
It never invents a value: every constant is read out of the pywin32 wheel.

    python3 tools/gen_win32con.py            # downloads the pinned pywin32 wheel
    python3 tools/gen_win32con.py --check    # verify the checked-in file is current

The wheel is a Windows wheel; it is only unzipped, never installed. `win32con` itself is
pure Python, so importing it off the unzipped tree works on Linux.
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import re
import sys
import urllib.request
import zipfile

PYWIN32_VERSION = '311'
REPO = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO / 'ok' / 'compat' / 'win32con_constants.py'

# Grouping is presentation only. Any name used by the tree but absent here is appended to
# an "Ungrouped" section, so the generator can never silently drop a constant.
GROUPS: list[tuple[str, list[str]]] = [
    ('Clipboard formats', ['CF_DIB', 'CF_UNICODETEXT']),
    ('Console control events',
     ['CTRL_C_EVENT', 'CTRL_CLOSE_EVENT', 'CTRL_LOGOFF_EVENT', 'CTRL_SHUTDOWN_EVENT']),
    ('Window styles / GetWindowLong indices',
     ['GWL_EXSTYLE', 'GWL_STYLE', 'WS_CAPTION', 'WS_OVERLAPPED', 'WS_POPUP', 'WS_SYSMENU']),
    ('GetWindow / ShowWindow / SetWindowPos',
     ['GW_HWNDNEXT', 'GW_OWNER', 'HWND_NOTOPMOST', 'HWND_TOPMOST', 'SWP_FRAMECHANGED',
      'SWP_NOMOVE', 'SWP_NOSIZE', 'SWP_SHOWWINDOW', 'SW_RESTORE', 'SW_SHOW']),
    ('Icons / images',
     ['IDI_APPLICATION', 'IMAGE_ICON', 'LR_DEFAULTSIZE', 'LR_LOADFROMFILE']),
    ('Mouse key state (wParam of the WM_*BUTTON* / WM_MOUSEMOVE family)',
     ['MK_LBUTTON', 'MK_MBUTTON', 'MK_RBUTTON', 'WHEEL_DELTA']),
    ('Monitors / metrics / blitting',
     ['MONITOR_DEFAULTTONEAREST', 'SM_CXSCREEN', 'SM_CYSCREEN', 'SRCCOPY']),
    ('Window messages',
     ['WM_ACTIVATE', 'WM_CHAR', 'WM_CLOSE', 'WM_DESTROY', 'WM_KEYDOWN', 'WM_KEYUP',
      'WM_LBUTTONDOWN', 'WM_LBUTTONUP', 'WM_MBUTTONDOWN', 'WM_MBUTTONUP', 'WM_MOUSEMOVE',
      'WM_MOUSEWHEEL', 'WM_RBUTTONDOWN', 'WM_RBUTTONUP', 'WM_SETFOCUS', 'WM_USER',
      'WA_ACTIVE', 'WA_INACTIVE']),
]

VK_GROUP_TITLE = (
    'Virtual-key codes -- the load-bearing half. keys.py builds vk_key_dict from these and\n'
    '# post_message.py looks up every keypress in it, so a stub here would be posted to the\n'
    '# game as a virtual-key code with no exception raised anywhere.'
)

HEADER = '''r"""The subset of `win32con` that ok-script actually references, for Linux.

GENERATED FILE -- do not edit by hand. Regenerate with `python3 tools/gen_win32con.py`,
which transcribes the values out of pywin32 {version}'s `win32/lib/win32con.py`.

Why this is real constants and not a `_Missing` stub: `win32con`'s members are integers
used in bit arithmetic and, critically, as the *values* of `vk_key_dict` in
`ok/device/interaction_methods/keys.py`. A stub does not raise there -- it silently makes
every virtual-key code a stub object, and the input backend then posts garbage.

The name set is exactly what `grep -rhoP "win32con\\.\\w+" --include=*.py ok` yields
({count} names). Anything else raises `AttributeError` from `__getattr__` naming the
constant, so an upstream rebase that starts using a new one fails loudly here rather than
somewhere far away.
"""

'''

FOOTER = '''

def __getattr__(name):
    raise AttributeError(
        f"win32con.{name} is not in ok-script's Linux win32con subset. Upstream started "
        f"using a new constant; regenerate with `python3 tools/gen_win32con.py` after "
        f"adding it to GROUPS."
    )
'''


def used_names() -> list[str]:
    """Every `win32con.X` referenced under ok/."""
    names = set()
    pat = re.compile(r'(?<![\w.])win32con\.(\w+)')
    for path in sorted((REPO / 'ok').rglob('*.py')):
        if path.resolve() == OUT.resolve():
            continue
        names.update(pat.findall(path.read_text(encoding='utf-8')))
    return sorted(names)


def pywin32_values(names: list[str]) -> dict[str, int]:
    """Read the constants out of the pywin32 win_amd64 wheel (never installed, only unzipped)."""
    api = f'https://pypi.org/pypi/pywin32/{PYWIN32_VERSION}/json'
    meta = json.load(urllib.request.urlopen(api, timeout=60))
    wheels = [u for u in meta['urls']
              if u['packagetype'] == 'bdist_wheel' and 'win_amd64' in u['filename']]
    if not wheels:
        raise SystemExit(f'no win_amd64 wheel published for pywin32 {PYWIN32_VERSION}')
    blob = urllib.request.urlopen(wheels[0]['url'], timeout=300).read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        source = zf.read('win32/lib/win32con.py').decode('utf-8', 'replace')

    # win32con is pure Python and imports nothing, so executing it on Linux is safe.
    namespace: dict[str, object] = {}
    exec(compile(source, 'win32con.py', 'exec'), namespace)

    missing = [n for n in names if n not in namespace]
    if missing:
        raise SystemExit(f'not in pywin32 {PYWIN32_VERSION}: {missing}')
    bad = {n: namespace[n] for n in names if not isinstance(namespace[n], int)}
    if bad:
        raise SystemExit(f'non-integer constants, refusing to emit: {bad}')
    return {n: namespace[n] for n in names}


def render(values: dict[str, int]) -> str:
    remaining = dict(values)
    chunks: list[str] = []

    def emit(title: str, names: list[str]) -> None:
        present = [n for n in names if n in remaining]
        if not present:
            return
        chunks.append('# ' + title)
        for n in present:
            v = remaining.pop(n)
            chunks.append(f'{n} = {v if -0x10 < v < 0x10 else hex(v)}')
        chunks.append('')

    for title, names in GROUPS:
        emit(title, names)
    emit(VK_GROUP_TITLE, sorted(n for n in remaining if n.startswith('VK_')))
    emit('Ungrouped -- added by an upstream rebase; sort these into GROUPS',
         sorted(remaining))
    assert not remaining, remaining

    body = '\n'.join(chunks).rstrip() + '\n'
    return (HEADER.format(version=PYWIN32_VERSION, count=len(values)) + body + FOOTER)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='exit non-zero if the checked-in file is stale')
    args = ap.parse_args()

    names = used_names()
    text = render(pywin32_values(names))

    if args.check:
        current = OUT.read_text(encoding='utf-8') if OUT.exists() else ''
        if current != text:
            print(f'{OUT} is stale ({len(names)} constants used); rerun without --check')
            return 1
        print(f'{OUT} is current ({len(names)} constants)')
        return 0

    OUT.write_text(text, encoding='utf-8')
    print(f'wrote {OUT} ({len(names)} constants from pywin32 {PYWIN32_VERSION})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
