# ok-script-linux

A fork of [ok-oldking/ok-script](https://github.com/ok-oldking/ok-script) that runs on
Linux. Base: upstream tag **v2.0.5**. Version string: `2.0.5+linux.N` — the PEP 440 local
segment satisfies every `ok-script>=2.0.5` pin while never being confused for the
published wheel.

The port adds a capture backend, an input backend, and a compatibility layer. It is not a
rewrite: upstream is already pluggable (`BaseCaptureMethod` / `BaseInteraction` with
BitBlt, WGC, DXGI, ADB, browser and NemuIPC backends), so Linux is one more backend pair.

## Status

| Phase | | |
|---|---|---|
| 0 | Fork, upstream remote, rebase-friendly layout | done |
| 1 | `import ok` and every lazily-mapped symbol work on Linux | done |
| 2 | `X11Window` — window discovery/geometry via python-xlib | not started |
| 3 | `X11CaptureMethod` — `XGetImage` + MIT-SHM | not started |
| 4 | `WinePostMessageInteraction` + the in-prefix `PostMessage` shim | not started |

Phase 1 makes the tree *importable, startable and testable* on Linux. It does not yet
make the app *useful*: `ok/util/window.py` still holds the Windows implementations of
`find_hwnd` / `get_window_bounds`, which import cleanly here but raise
`NotImplementedError` naming the symbol if called. Phase 2 replaces those bodies.

Where startup stops today, with ok-ww's config on Linux:

```
OK(config) -> DeviceManager.__init__ -> HwndWindow.__init__
           -> hwnd_window.py:392 get_monitors_bounds()
           -> NotImplementedError: win32api.EnumDisplayMonitors
```

That is Phase 2's first line of work (XRandR via python-xlib), and everything before it —
config load, game-install detection, the single-instance lock, the whole lazy-import
graph, `DeviceManager` construction — now runs.

## Rebasing onto a new upstream tag

```sh
git fetch upstream --tags
git rebase v2.0.6            # or whichever tag
python3 tools/scan_module_level_win32.py --check   # did the Win32 import surface move?
python3 tools/gen_win32con.py --check              # did a new win32con constant appear?
python3 tools/check_linux_imports.py               # do all lazy symbols still resolve?
python3 -m pytest tests
```

All four are cheap and each one has caught a real regression. Run them in that order: the
scanner tells you whether the *shape* of the problem changed before the others tell you
that something broke. `.github/workflows/linux.yml` runs the same set on `ubuntu-latest`,
so a rebase that skips this locally is still caught.

`gen_win32con.py --check` is the one exception: it downloads the pywin32 311 `win_amd64`
wheel from PyPI on **every** invocation, `--check` included, so it needs the network and
fails on an offline or rate-limited machine for reasons unrelated to the tree. CI runs it
as a separate non-blocking job for that reason.

Then bump `FORK_VERSION` in `setup.py` to the new base.

## What Phase 1 added

Everything Linux-specific lives in new files. Existing upstream files are touched only
where there was no alternative, to keep rebases cheap:

| File | Change |
|---|---|
| `ok/compat/win32_stub.py` | **new** — makes Windows-only import-time names exist |
| `ok/compat/win32con_constants.py` | **new**, generated — the 94 `win32con` constants the tree uses, with real values |
| `ok/compat/winreg_constants.py` | **new**, generated — the `HKEY_* / KEY_* / REG_*` integers |
| `ok/compat/single_instance.py` | **new** — `flock` single-instance lock, in place of the Windows named mutex |
| `ok/device/capture_methods/geometry.py` | **new** — `get_crop_point` / `parse_reg_flag`, split out of the win32-flavoured `bitblt_utils` so the Linux capture path can use them |
| `conftest.py` | **new** — installs the shim before pytest collection |
| `tools/` | **new** — the three checks above |
| `pyproject.toml` | platform markers on `pywin32`, `pydirectinput`, `pycaw`, `ok-d3dshot`; adds `python-xlib` on Linux |
| `setup.py` | version no longer derived from a live PyPI query |
| `ok/__init__.py` | calls `win32_stub.install()` on non-win32; POSIX signal handlers in place of `SetConsoleCtrlHandler` |
| `ok/device/capture_methods/bitblt_utils.py` | re-exports the two moved functions |
| `ok/util/process.py` | POSIX branch in `check_mutex`, same wait / identify-owner / terminate policy |

### The compatibility shim

`ok/compat/win32_stub.py` gives Linux the names 27 modules read at import scope. Imports
succeed; a code path that genuinely needs Windows raises `NotImplementedError` naming the
symbol, rather than silently no-opping. Five details are load-bearing:

* **DLL loaders must not raise.** Four modules call one *while importing*
  (`ok/util/window.py:18`, `ok/rotypes/roapi.py:7`, `ok/rotypes/winstring.py:6`,
  `ok/rotypes/Windows/Foundation/__init__.py:9`), so loader-shaped calls return a handle
  stub.
* **`win32con` carries real integers, never stubs.** Its members are the *values* of
  `vk_key_dict` in `ok/device/interaction_methods/keys.py`. A stub does not raise there —
  it silently turns every virtual-key code into a stub object and the input backend posts
  garbage. Generated from pywin32 by `tools/gen_win32con.py`; unknown names raise.
* **Attribute access memoises.** `ctypes.windll.user32` must be the same object twice, as
  it is in real ctypes — otherwise `mock.patch('…ctypes.windll.user32.GetDpiForWindow')`
  patches a throwaway and silently does nothing.
* **`win32api.MAKELONG` is implemented, not stubbed.** It is a C macro, not an OS call,
  and it is on the hot input path.
* **`winreg` calls raise `FileNotFoundError`, and its constants are real.** Callers guard
  registry lookups two ways — `except OSError` (ok-ww `config.py`,
  `ok/alas/emulator_windows.py:34,50`) and `except FileNotFoundError`
  (`emulator_windows.py`, eleven lookups). Only `FileNotFoundError` satisfies both, and it
  is what real winreg raises for a missing key. A bare `OSError` escaped all eleven and
  took down `EmulatorManager().all_emulator_instances`; `NotImplementedError` escaped both
  and took down ok-ww's game-install detection. Callers also combine the constants, so
  `winreg.KEY_READ | winreg.KEY_WOW64_64KEY` must not be a `TypeError`.

`ok.rotypes` and `ok.capture.windows` cannot be made importable on Linux and are not meant
to be — `ok/rotypes/inspectable.py:12` uses the COM vtable prototype form
`WINFUNCTYPE(...)(0, "QueryInterface")`, which `CFUNCTYPE` rejects. Harmless: both are only
ever imported from inside function bodies that do not run here. Keep them out of any
import sweep.

## Test baseline on Linux

`opencv-python` must be installed alongside the extras. ~14 modules (`ok/util/color.py`,
`DeviceManager`, `FeatureSet`, `ok/core/screenshot.py`, `bitblt_utils` ...) `import cv2` at
module scope, but upstream deliberately does not declare it — `tests/test_package_metadata.py`
asserts that no profile mentions opencv, so that a headless consumer can choose
`opencv-python-headless` instead. Downstream ok-ww declares it. Without it you get 20
collection errors and `check_linux_imports.py` reports 35/70 failed, all
`No module named 'cv2'` — which reads exactly like a port regression and is not one.

```sh
pip install -e '.[web,default,qt,adb,ocr,dev]' pytest-qt opencv-python
QT_QPA_PLATFORM=offscreen python3 -m pytest tests
```

Do **not** add `-q`: `pytest.ini`'s `addopts` already carries it, and a second `-q` raises
quiet to level 2, which suppresses the final `N failed, M passed` line entirely. The run
still exits 1 and its last visible line is a `FAILED` row, which looks like a truncated or
crashed run and is not.

Baseline: **376 passed, 6 failed, 1 skipped, 10 subtests passed** (392 collected, Python
3.12). Reproducible run to run — the suite used to be flaky across files, with 2-6 extra
failures drifting between runs of the same command, because `TaskTab`'s 1s `QTimer` was
unparented and outlived its widget, firing `og.executor.current_task` into whatever test
was running next. It is now parented to the tab and guards on `og.executor is None`.

The six failures are Windows-only by construction, not port regressions:

| Test | Why |
|---|---|
| `test_device_manager` — `test_resolves_mumu_{12,15}_instance_window` | MuMu emulator paths; `os.path.split` does not treat `\` as a separator on POSIX |
| `test_process` — `test_execute_can_use_os_startfile_when_configured` | `os.startfile` is Windows-only; same backslash-path issue |
| `test_task_ui` — `test_task_card_uses_single_line_compact_header` | asserts an exact Qt pixel height; font metrics differ per platform |
| `test_web_server` — `test_native_resize_handle_is_invisible_but_not_click_through`, `test_rounded_window_region_tracks_native_window_size` | pywebview WinForms / win32 window shaping |

None sit on the game path. Re-check this list after a rebase; a *new* failure outside it is
a regression. CI deselects exactly these six by node id — keep the two lists in step.
