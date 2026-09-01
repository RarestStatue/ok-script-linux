"""Make the Windows-only names ok-script touches at *import* time exist on Linux.

`install()` must run before the first `import ok.*` that touches Windows. `ok/__init__.py`
calls it at the top of its own body on non-win32, before importing anything else from
`ok.*`, so in practice a plain `import ok` bootstraps this and no caller has to remember
to. It is not lazy and cannot be: several modules do `from ctypes import *` or
`from ctypes import windll` at module scope, and four of them go further and *call* a DLL
loader while importing:

    ok/util/window.py:18                          ctypes.WinDLL('user32', use_last_error=True)
    ok/rotypes/roapi.py:7                         windll.LoadLibrary('combase.dll')
    ok/rotypes/winstring.py:6                     ctypes.windll.LoadLibrary('combase.dll')
    ok/rotypes/Windows/Foundation/__init__.py:9   windll.LoadLibrary('kernel32.dll')

So `_Missing.__call__` returns another `_Missing` for loader-shaped names instead of
raising -- a DLL *handle* stub. Every other call raises `NotImplementedError` naming the
symbol, which is the point: imports succeed, and any path that genuinely needs Windows
fails loudly and locally instead of silently no-opping.

Two names must be real objects rather than stubs:

* `win32con` -- its members are integers used in bit arithmetic and as the *values* of
  `vk_key_dict` in `ok/device/interaction_methods/keys.py`. A `_Missing` does not raise
  there; it silently turns every virtual-key code into a stub object and the input backend
  posts garbage. See `ok.compat.win32con_constants`.
* `ctypes.HRESULT` / `ctypes.WINFUNCTYPE` -- `ok/rotypes/delegate.py:9-11` uses them as
  *types* at module scope, so a `_Missing` raises during import.

`winreg` calls raise `FileNotFoundError` rather than `NotImplementedError` (see
`_CALL_ERRORS`), and its module carries the real `HKEY_* / KEY_* / REG_*` integers, because
callers combine them -- `winreg.KEY_READ | winreg.KEY_WOW64_64KEY` is a `TypeError` against
stubs.

Assumption, audited 2026-09-01: `install()` sets `ctypes.windll`, `.oledll`, `.WinDLL`,
`.OleDLL`, `.HRESULT` and `.WINFUNCTYPE` on the *global* `ctypes` module, so any library
that platform-sniffs with `hasattr(ctypes, 'windll')` would conclude it is on Windows.
No installed dependency does: `psutil`, `pynput`, `mouse`, `pyappify`, `darkdetect`,
`pywebview`, `setuptools` and the PySide6 stack all branch on `platform.system()` or
`sys.platform`. Re-check this when a dependency is added; a false positive here shows up
as a third-party library taking a Windows code path and failing far from this file.

`ok.rotypes` and `ok.capture.windows` still cannot be imported on Linux and are not meant
to be: `ok/rotypes/inspectable.py:12` uses the COM vtable prototype form
`WINFUNCTYPE(...)(0, "QueryInterface")`, which `CFUNCTYPE` rejects, and there is no
pure-Python substitute. That is harmless -- both packages are only ever imported from
*inside* functions (`ok/util/window.py:windows_graphics_available()`,
`ok/device/capture_methods/windows_graphics.py:189-196,241`) whose bodies do not run on
Linux, because `WINDOWS_BUILD_NUMBER == -1` and WGC is not in the Linux capture-method
list. Do not add them to any import sweep.
"""

import ctypes
import sys

# Leaf names whose *call* must succeed at import time and yield another stub.
_LOADERS = ('LoadLibrary', 'WinDLL', 'OleDLL', 'CDLL', 'windll', 'oledll')

# Stubbed as opaque `_Missing` modules: every symbol ok-script uses from these is *called*,
# so a stub is the correct shape -- it raises, naming the symbol, if a Windows-only code
# path is ever reached on Linux.
_STUB_MODULES = (
    'win32api', 'win32gui', 'win32process', 'win32ui', 'win32file', 'win32clipboard',
    'pythoncom', 'pydirectinput', 'pycaw', 'comtypes', 'd3dshot', 'winreg',
)

# Modules whose calls should raise something other than NotImplementedError.
#
# `winreg` is the one that matters. Callers guard it two ways: `try: import winreg /
# except ImportError`, which a stubbed module defeats, and an `except` around each lookup,
# which is how real winreg reports a missing key. On Linux there is genuinely no registry,
# so raising the missing-key error is not a fudge -- it is the accurate answer, and it puts
# every caller on the "nothing registered" path they already handle.
#
# It must be `FileNotFoundError`, not a bare `OSError`, because the two guard styles in the
# tree are not interchangeable in that direction:
#
#   except OSError            ok-ww `config.py:_find_most_recently_run_pc_exe`,
#                             `ok/alas/emulator_windows.py:34,50`
#   except FileNotFoundError  `ok/alas/emulator_windows.py`, 11 lookups (203, 228, 233,
#                             241, 374, 387, 406, 431, 437, 478, 486)
#
# `FileNotFoundError` is a subclass of `OSError`, so it satisfies both -- and it is what
# real winreg raises for a missing key, so it is also the more accurate stand-in. A bare
# `OSError` escapes all eleven `except FileNotFoundError` guards and takes down
# `EmulatorManager().all_emulator_instances`.
_CALL_ERRORS = {
    'winreg': FileNotFoundError,
}

_installed = False


def _MAKELONG(low, high):
    """`win32api.MAKELONG` -- a C macro, not an OS call, so implement it rather than raise.

    It is on the hot input path: `post_message.py` and `genshin.py` pack every click and
    wheel coordinate through it.
    """
    return ((int(high) & 0xFFFF) << 16) | (int(low) & 0xFFFF)


# Windows symbols that are pure arithmetic. Stubbing these would be a false negative: they
# have exact, portable definitions and the code below calls them on Linux for real.
def _winreg_constants():
    from ok.compat import winreg_constants
    return {n: v for n, v in vars(winreg_constants).items() if not n.startswith('_')}


# Windows symbols that must be real rather than stubbed -- either pure arithmetic, or
# integers the callers combine. Filled in by install(), since some need an import.
_REAL_IMPLEMENTATIONS = {
    'win32api': lambda: {'MAKELONG': _MAKELONG},
    'winreg': _winreg_constants,
}


class _Missing:
    """A Windows symbol that does not exist here. Attribute access chains; calls raise."""

    # Deliberately no __slots__: `unittest.mock.patch.object` (and any monkeypatching of
    # a stubbed module) has to be able to setattr onto these, exactly as it can onto a
    # real `win32gui`. With __slots__ that fails as
    # `AttributeError: '_Missing' object has no attribute 'GetClientRect'`.

    def __init__(self, path, attrs=None, error=NotImplementedError):
        self._path = path
        self._error = error
        if attrs:
            self.__dict__.update(attrs)

    def __repr__(self):
        return f'<win32 stub {self._path!r} (unavailable on {sys.platform})>'

    def __getattr__(self, name):
        # Dunder lookups must fail normally, or copy/pickle/inspect/unittest.mock get
        # handed a _Missing where they expect a real protocol method. The two own fields
        # are listed so a half-initialised instance cannot recurse through __getattr__.
        if name in ('_path', '_error') or (name.startswith('__') and name.endswith('__')):
            raise AttributeError(name)
        child = _Missing(f'{self._path}.{name}', error=self._error)
        # Memoise into __dict__, so `ctypes.windll.user32` is the *same* object every
        # time -- as it is in real ctypes. Without this, `patch('pkg.ctypes.windll.'
        # 'user32.GetDpiForWindow', ...)` patches a throwaway and silently does nothing,
        # and `__getattr__` would stop being consulted for a name someone has set.
        setattr(self, name, child)
        return child

    def __call__(self, *args, **kwargs):
        leaf = self._path.rsplit('.', 1)[-1]
        if leaf in _LOADERS:
            return _Missing(f'{self._path}(...)', error=self._error)
        raise self._error(
            f'Windows-only symbol called on {sys.platform}: {self._path}'
        )


def install():
    """Patch `ctypes` and register the stub modules. Idempotent; a no-op on Windows."""
    global _installed
    if sys.platform == 'win32' or _installed:
        return
    _installed = True

    # --- ctypes: names Linux CPython does not define -----------------------------------
    # `ctypes` declares no `__all__`, so `from ctypes import *` re-exports these too --
    # which is what lets ok/rotypes/{roapi,inspectable}.py get as far as they get.
    ctypes.windll = _Missing('ctypes.windll')
    ctypes.oledll = _Missing('ctypes.oledll')
    ctypes.WinDLL = _Missing('ctypes.WinDLL')
    ctypes.OleDLL = _Missing('ctypes.OleDLL')
    # Used as types at module scope, so these must be real. pywin32 and ok/rotypes both
    # define HRESULT as LONG.
    ctypes.HRESULT = ctypes.c_long
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE

    # --- modules -----------------------------------------------------------------------
    for name in _STUB_MODULES:
        build_attrs = _REAL_IMPLEMENTATIONS.get(name)
        sys.modules.setdefault(name, _Missing(
            name,
            build_attrs() if build_attrs else None,
            error=_CALL_ERRORS.get(name, NotImplementedError),
        ))

    # win32con is the one that must carry real values -- see the module docstring.
    from ok.compat import win32con_constants
    sys.modules.setdefault('win32con', win32con_constants)
