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
| 2 | `X11Window` — window discovery/geometry via python-xlib | done |
| 3 | `X11CaptureMethod` — `XGetImage` + MIT-SHM | done |
| 4 | `WinePostMessageInteraction` + the in-prefix `PostMessage` shim | done |

Phase 1 made the tree *importable, startable and testable* on Linux. Phase 2 makes the
window layer *real*: the app finds the game's X11 window, tracks its geometry, focus and
minimized state, mutes it in the background, and hands `DeviceManager` a PC device. Phase 3
makes the pixels real: `X11CaptureMethod` grabs the game's own window through MIT-SHM.
Phase 4 makes input real: `WinePostMessageInteraction` writes to a small C program running
*inside* the game's Proton prefix, which does the `PostMessageW`.

With ok-ww's config on Linux, startup now reaches:

```
OK(config) -> DeviceManager -> X11Window -> find_hwnd -> do_start
           -> update_capture_method(['X11', 'X11_Composite'])
           -> X11CaptureMethod, which produces real frames of the game
           -> WinePostMessageInteraction -> okww-input-shim.exe -> the game's HWND
```

Capture was measured against Wuthering Waves running under Proton: 2560x1440 at
4.5 ms/frame, live while the window was occluded. Reproduce the startup path with ok-ww's
`tools/check_linux_startup.py` and the input path with ok-ww's `tools/check_shim.py` (both
live in ok-ww: one needs ok-ww's config, the other the shim's C source and built exe).

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

## What Phase 2 added

| File | Change |
|---|---|
| `ok/compat/x11.py` | **new** — the python-xlib window layer: enumeration, `_NET_WM_PID`, geometry, focus, minimized state, RandR monitors, activate, resize. Nothing in it raises; every entry point has a documented empty return |
| `ok/compat/window_x11.py` | **new** — the Linux bodies of the `ok.util.window` contracts (`find_hwnd`, `get_window_bounds`, `is_foreground_window`, `resize_window`, `find_all_visible_windows`, `show_title_bar`, `get_exe_by_hwnd`, `is_window_minimized`) |
| `ok/device/capture_methods/x11_window.py` | **new** — `X11Window`, plus `get_monitors_bounds` and the pactl-backed `get_mute_state` / `set_mute_state` |
| `tests/test_x11_window.py` | **new** — 74 tests: tuple-shape contracts, the two semantics the plan got wrong first time, two drift gates (the copied constructor, and the win32-bound methods that must stay overridden), the `resize_window` window-rect contract, the two replyless-request contracts, the `WM_STATE` focus resolution a reparenting WM needs, and live tests against a real X server |
| `ok/device/capture_methods/__init__.py` | rebinds `HwndWindow` to `X11Window` on Linux, and shadows the five helpers line 21 imports from `hwnd_window` |
| `ok/util/window.py` | imports the X11 bodies over the Win32 ones on non-Windows, at the bottom of the file |
| `ok/core/screenshot.py` | the annotation font is looked up per platform; `os.environ['WINDIR']` is a `KeyError` here |

`X11Window` subclasses `HwndWindow` and overrides only the Win32-bound methods — the
eleven pure ones (`get_abs_cords`, `get_capture_origin`, `get_top_window_cords`,
`capture_target_signature`, `update_window`, `update_frame_size`, `frame_ratio`, `stop`,
`_front_hwnd_candidates`, `_top_hwnd_info`, `__str__`) stay upstream's across rebases.
`__init__` is a copy, because upstream's calls `get_monitors_bounds()` out of its own
module globals; `TestUpstreamDrift` walks both ASTs and fails if upstream's constructor
grows an attribute the copy does not set, or if a method that reaches Win32 — directly,
through the module's own helpers, or through the `ok.util.window` contracts — is left
inherited rather than overridden. The method half used to ask `hasattr(X11Window, name)`,
which is True by definition for a subclass: it could not fail, so an upstream rebase that
added a Win32-calling method would have landed as a silently inherited
`NotImplementedError` with a green suite.

Three semantics are load-bearing and easy to get backwards:

* **`visible` means foreground, not mapped.** Upstream sets `visible = self.is_foreground()`,
  and `MouseResetTask` pins the physical cursor only while `not hwnd.visible` — i.e.
  exactly during background play. A mapped-based `visible` is True all session and silently
  disables it.
* **Iconic is a separate signal.** An iconified X11 window keeps its geometry, so
  `check_pos` alone never notices; `pos_valid` gets an explicit minimized test, which is
  what pauses the executor and shows the "game window is minimized" notification.
* **Identity is `_NET_WM_PID` plus the process command line.** Every Proton window's
  `WM_CLASS` is `steam_proton`, and the Win32 class (`UnrealWindow`) is invisible from
  X11, so `class_name` / `top_hwnd_class` are accepted and ignored. The game's name lives
  only in the Wine command line, which is why `find_hwnd` matches against every `.exe` on
  it rather than against `/proc/<pid>/exe`.

Three more, found by review after Phase 2 landed and fixed in the same tree:

* **`resize_window`'s `width`/`height` are the *window* rect, decorations included.** That
  is what the Windows body means — `SetWindowPos` sizes the window rect and the settle
  check reads `GetWindowRect` — and what both callers pass. X11 has no window rect (the
  client window *is* the client area), so the function takes `_NET_FRAME_EXTENTS` off
  before configuring and adds them back for the settle check. Sizing the client to those
  numbers instead made `try_resize_to` overshoot by a title bar and then report failure
  despite the WM obeying, and made `start_controller`'s re-centre path grow the window by
  the frame extents on *every* call, unboundedly.
* **`pactl` is localized; its output is parsed, so its environment is pinned to `LC_ALL=C`
  (and `LANGUAGE=''`, which overrides `LC_ALL` for gettext).** In de_DE the header is
  `Ziel-Eingabe #` and the flag is `Stumm:`; in zh_CN, `信宿输入 #`. Unpinned, `pactl`
  exits 0 with output on stdout and the parser returns nothing — mute fails silently, and
  the option looks like it works.
* **`x11.activate()` measures the answer instead of assuming it.** Every request it issues
  is replyless and their errors arrive asynchronously, so the old `return True` after
  `sync()` reported success even for a window id that had never existed. It polls
  `is_active` for half a second and returns that. The de-iconify half (the `MapWindow`
  that stands in for `ShowWindow(SW_RESTORE)`) happens either way, so a focus-stealing
  refusal still restores the window. `x11.resize()` is the same shape and got the cheaper
  half of the same treatment: its `ConfigureWindow` is replyless too, so it asks for the
  window's attributes first — a reply-bearing request — which makes a dead window a
  synchronous `BadWindow` instead of a True that `resize_window` then spends its full
  five-second settle loop disproving.

`find_hwnd` returns `[]` for its `hwnds` element where Windows returns `[biggest]`: Wine
gives one X toplevel per game, so there is no child/top window to report. All four
consumers handle the empty list. `real_width` / `real_height` are the window's size, never
0 — zeros give `DeviceManager` a `0x0` device and freeze change detection.

## What Phase 3 added

| File | Change |
|---|---|
| `ok/compat/xshm.py` | **new** — the pixel path: libX11/libXext/libXcomposite through `ctypes`, MIT-SHM with an `XGetImage` fallback, an error handler that keeps a protocol error from exiting the process, and BGRA→BGR unpacking |
| `ok/device/capture_methods/x11_capture.py` | **new** — `X11CaptureMethod`, the crop rectangle, and `x11_capture_available()` |
| `tests/test_x11_capture.py` | **new** — 34 tests: pixel-format unpacking over hand-made `XImage` structs, the crop contract against `get_capture_origin`, the selection branch, and live grabs of a real window through both the shared-memory and the wire path |
| `ok/device/capture_methods/update.py` | the `X11` / `X11_Composite` branch, next to WGC and BitBlt |
| `ok/device/capture_methods/__init__.py` | exports `X11CaptureMethod` |

`python-xlib` owns the window layer and `ctypes` owns the pixel layer, on separate display
connections. That split is forced: python-xlib has no MIT-SHM binding at any version, and
`XShmGetImage` is 6.6x cheaper than `XGetImage` at 2560x1440 (4.5 ms against 29.7 ms,
measured against the game).

Five things are load-bearing:

* **Xlib's default error handler calls `exit(1)`.** A window that dies between the poll
  thread reading its geometry and the capture thread grabbing it is a routine `BadWindow`;
  without `XSetErrorHandler` the first one takes the app down. The handler is
  process-global rather than per-display, which is only acceptable because nothing else
  here talks to libX11 — PySide6 uses xcb, python-xlib speaks the protocol itself.
* **The returned frame is a copy, made by `cv2.cvtColor`.** The shared segment is
  overwritten by the next grab while `TaskExecutor` still holds the previous frame. At
  1080p: `arr[:, :, :3].copy()` 10.10 ms, `np.ascontiguousarray` 9.69 ms, `cv2.cvtColor`
  0.15 ms.
* **A grab from a Pixmap comes back with the image's RGB masks zeroed.** Both `XGetImage`
  and `XShmGetImage` fill them in from the reply's visual id, and a pixmap has no visual —
  so the composite path has to carry the window's own visual masks down to the unpacking.
* **The composite path re-names the pixmap on every grab.** A name is a handle onto the
  backing pixmap *as it is now*, and a client that presents by flipping (DXVK, for the
  game) gets a new one per frame. Cached, six grabs 0.25 s apart differed by exactly 0.0 —
  a frozen picture that looks like a working capture. Re-named, 28-57. It costs 3.6 → 5.4
  ms/frame, which is what the direct path costs anyway.
* **A minimized window returns None, it does not raise.** A `CaptureException` out of a
  task reaches `TaskExecutor.py:639` and is answered with `task.disable()`, so raising here
  would turn a minimize into a task the user has to switch back on by hand. The window
  layer already pauses the executor and notifies, reversibly, when `pos_valid` goes False
  (`x11_window.py:410-417`). The report is still gated on `x11.exists()` first, because
  `is_minimized()`'s last resort is "not viewable", which a window id that no longer exists
  answers True — and it is logged once per episode, not once per poll.

### System libraries

The pixel path is `ctypes` over the system X libraries, so they are a **runtime
requirement** that no Python lock can express:

| | Fedora | Debian / Ubuntu |
|---|---|---|
| required | `libX11`, `libXext` | `libx11-6`, `libxext6` |
| optional (`X11_Composite` only) | `libXcomposite` | `libxcomposite1` |

Without `libX11`/`libXext`, `x11_capture_available()` is False, both `X11` and
`X11_Composite` are skipped, `update_capture_method` returns None and the app starts with
no capture method — with one log line as the only trace
(`libX11/libXext are not loadable, X11 capture is unavailable`). Without `libXcomposite`,
`X11` works and `X11_Composite` degrades to it. CI gets all three transitively through
`xvfb`, which is why nothing noticed until it was looked for.

`X11` grabs the window directly and `X11_Composite` grabs an XComposite offscreen pixmap.
The second is for a plain non-compositing X server, where an occluded window's pixels are
genuinely not in the framebuffer; under Xwayland or any compositing WM the direct path
already captures an occluded window, verified against the game with a window covering it.

## What Phase 4 added

| File | Change |
|---|---|
| `ok/compat/proton_shim.py` | **new** — Steam library / `appmanifest` / `config_info` parsing, the shim's launch shapes (`proton run` and the SteamLinuxRuntime entry point), the handshake file, and the authenticated line client |
| `ok/device/interaction_methods/wine_post_message.py` | **new** — `WinePostMessageInteraction`: `PostMessageInteraction` method for method, over the socket |
| `tests/test_wine_post_message.py` | **new** — 60 tests: the Steam/Proton parsing against a fabricated tree, the protocol against a real loopback server, and the backend's semantics |
| `ok/device/interaction_methods/base.py` | `get_cursor_pos()` / `set_cursor_pos()` on the interface, so task code stops calling `win32api` directly |
| `ok/device/interaction_methods/__init__.py` | exports `WinePostMessageInteraction` |
| `ok/device/DeviceManager.py` | the `'WinePostMessage'` branch in **both** selection ladders — the constructor's and `set_interaction`'s, which the GUI picker uses |

The shim itself (`shim/okww-input-shim.c`, ~450 lines of C, built with mingw) lives in
ok-ww, next to its build instructions and the prebuilt exe.

Why a program inside the prefix at all: `PostMessage` delivers to an **unfocused** window,
which is what background play means, and nothing on the Linux side can do that —
`XSendEvent` is focus-bound and XTEST is global (it moves the real keyboard). The heavy
half of the app stays native: only a few KB of input logic runs under Wine.

Six things are load-bearing:

* **The hot path is fire-and-forget, in both directions.** Upstream's `post()` swallows
  every error and returns nothing, so no caller ever reads a result. The shim replies to
  `HELLO`/`FINDWIN`/`GEOM`/`GETCURSOR`/`VKKEYSCAN`/`PING`/`STATS`/`QUIT` and to nothing
  else — a reply nobody reads would fill the socket buffer and eventually block the shim
  mid-combat, and a round-trip per keypress would sit inside the combat loop and inside
  `swipe`, which issues up to 100 `move()` calls back to back.
* **Every reply carries its command as a tag** (`GEOM 0 0 2560 1440`, `ERR GEOM notfound`).
  Without it, one late or unsolicited line pairs every later answer with the wrong
  question, silently, for the rest of the session.
* **`MapVirtualKey` runs inside Wine.** The scan code in the keyboard lparam has to be the
  one the game's Unreal input layer expects, so it cannot be precomputed on Linux.
* **The Proton build comes from the prefix's own `config_info`.** Launching the shim with a
  *different* build than the game was last run with rewrites the prefix ("Upgrading prefix
  from X to Y"). The tool directory is recovered by walking up from one of the file's
  absolute paths to the directory holding `proton`, not by stripping a fixed suffix.
* **The connection is maintained by a thread, not by the sender.** ok-ww is normally
  started before the game; the first attempt fails with "the game is not running", and a
  link that only retried on the next `send` would drop the first seconds of play.
* **Two upstream bugs are fixed in the Linux backend and left alone on Windows.**
  `swipe`'s `steps = int(duration / 100)` is 0 at the default `duration=3` and then divides
  by it; `mouse_up` releases at `self.mouse_pos`, which is `(0, 0)` for the life of the
  object, so upstream ends every drag at client (0, 0). A third — `right_click` opening
  with `super().right_click(...)`, which `BaseInteraction` does not define — is dead on
  both platforms, because `Task.right_click` routes through `click(key='right')`.

### Security

The shim can synthesize input into the game, so it is reachable only from this machine and
only by this user: the listener binds `127.0.0.1` on a kernel-assigned port, the first line
of every connection must present a 32-byte random token, and that token is published only
through a handshake file inside the prefix that the Linux side pre-creates at mode 0600 and
truncates in place. Anything else is dropped without a reply.

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

Baseline: **549 passed, 6 failed, 1 skipped, 16 subtests passed** (556 collected, Python
3.12) — 376 of those passes predate Phase 2 (`tests/test_x11_window.py`, 74), Phase 3
(`tests/test_x11_capture.py`, 41) and Phase 4 (`tests/test_wine_post_message.py`, 60).
Reproducible run to run — the suite used to be flaky across files, with 2-6 extra
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

Twenty-three of the 491 are live X11 tests: they create a real window and drive it through a
real server — 13 in `test_x11_window.py` (`61 passed, 13 skipped` for that file with no
`DISPLAY`) and 10 in `test_x11_capture.py` (`31 passed, 10 skipped`). So CI runs the suite
under `xvfb-run`. Xvfb has no window manager, and the five tests that need one — iconify,
de-iconify-on-activate, the two `resize_window` ones, and the capture layer's iconify test —
skip themselves there; they run in full on a desktop session.
