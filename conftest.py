"""Belt and braces: make sure the Linux Win32 shim is installed before collection.

`ok/__init__.py` already calls `ok.compat.win32_stub.install()` at the top of its own body
on non-win32, and the `from ok.compat...` import below executes that first -- so by the
time this module's `install()` runs, `_installed` is already True and the call is a no-op.
This file is therefore *not* what makes pytest work, and removing `ok/__init__.py`'s call
in the belief that this covers it would break every entry point that is not pytest
(upstream's own `test_headless_imports` spawns `python -c "import ok"`).

It is kept only so that the ordering requirement stays visible at the root of the tree.
No-op on Windows.
"""

from ok.compat.win32_stub import install

install()
