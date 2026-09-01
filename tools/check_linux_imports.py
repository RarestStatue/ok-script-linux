#!/usr/bin/env python3
"""Phase 1 exit criterion: every lazily-mapped `ok` symbol must resolve on Linux.

`import ok` alone is a false green. `ok/__init__.py` is PEP-562 lazy: a `_LAZY_IMPORTS`
name -> (module, attr) map plus a module-level `__getattr__`, with the Win32-tainted
imports confined to `if TYPE_CHECKING:`. A bare `import ok` therefore succeeds on a
completely unported tree, pulling in only nine Windows-free modules. The explosion happens
later, when `OK.start()` resolves `MainWindow`, `DeviceManager`, `check_mutex`,
`windows_graphics_available` and friends -- which is what this script forces up front.

    python3 tools/check_linux_imports.py

Exit status is 0 only if every entry resolves. `ok.rotypes` and `ok.capture.windows` are
deliberately absent from `_LAZY_IMPORTS` and must stay out of any sweep: they cannot be
made importable on Linux (COM vtable prototypes need a real `WINFUNCTYPE`) and they are
only ever imported from inside function bodies that do not run here.
"""

from __future__ import annotations

import importlib
import sys

# Must precede the first `import ok.*`: several modules read Windows-only `ctypes` names,
# and four call a DLL loader, at module scope.
from ok.compat.win32_stub import install

install()

import ok  # noqa: E402
from ok import _LAZY_IMPORTS  # noqa: E402

# There is deliberately no skip list. An earlier version skipped `run_web` on *any*
# ModuleNotFoundError, on the theory that ok-script's 'web' extra might be absent -- which
# meant an unrelated missing module (observed: `cv2`) turned a genuine breakage into
# `SKIP` and let the gate exit 0. `run_web` resolves without the extra anyway, so the skip
# bought nothing. If an entry ever does need one, match on `exc.name`, never on the
# exception type.


def main() -> int:
    if sys.platform == 'win32':
        print('This check is for Linux; nothing to do on win32.')
        return 0

    failed: list[tuple[str, str, str, str]] = []
    resolved = 0

    for name, (module, attr) in sorted(_LAZY_IMPORTS.items()):
        try:
            getattr(importlib.import_module(module), attr)
            resolved += 1
        except Exception as exc:  # noqa: BLE001 - reporting, not handling
            failed.append((name, module, type(exc).__name__, str(exc)))

    for name, module, kind, message in failed:
        print(f'FAIL  {name:<32} {module}  {kind}: {message}')

    total = len(_LAZY_IMPORTS)
    print(f'\n{resolved}/{total} _LAZY_IMPORTS entries resolved, '
          f'{len(failed)} failed  (ok {ok.__file__})')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
