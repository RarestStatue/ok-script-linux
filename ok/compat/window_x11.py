"""Linux implementations of the ``ok.util.window`` contracts.

``ok/util/window.py`` is the choke point of the whole device layer: ``DeviceManager``,
``HwndWindow``, ``browser.py``, ``update.py``, ``desktop_duplication.py`` and
``TaskExecutor`` all import from it. Phase 1 made it *importable* on Linux (the win32 stub
turns ``ctypes.WinDLL('user32')`` at line 18 into a handle stub); this module replaces the
*bodies* that would otherwise raise, and ``ok/util/window.py`` shadows them at the bottom
of its own file on non-Windows.

Only the names that genuinely need X11 live here. Four of the module's eleven public
contracts are already correct on Linux and are deliberately **not** shadowed:
``find_display`` and ``ratio_text_to_number`` are pure logic, ``WGC_NO_BORDER_MIN_BUILD``
is a constant, and ``WINDOWS_BUILD_NUMBER`` is upstream's own ``-1`` on non-win32 -- which
is what makes ``windows_graphics_available()`` short-circuit and never touch
``ok.rotypes`` (unimportable here by design). ``windows_graphics_available`` is likewise
left alone: it falls off the end and returns ``None``, falsy at every call site.

Two documented deviations from the Windows behaviour, both forced by what X11 can see:

* **``class_name`` and ``top_hwnd_class`` are ignored.** They name *Win32* window classes
  (ok-ww passes ``UnrealWindow``, plus regexes for the login dialogs), which exist only
  inside Wine and are invisible from X11 -- every Proton window's ``WM_CLASS`` is
  ``steam_proton``. Honouring them would match nothing, ever. Identity comes from
  ``_NET_WM_PID`` plus the process command line instead.
* **``find_hwnd`` returns ``[]`` for ``hwnds``**, where Windows returns ``[biggest]``.
  Wine gives one X toplevel per game, so there is no child/top window distinction to
  report. All four consumers handle the empty list; ``_top_hwnd_info([])`` is ``None``,
  which makes ``top_hwnd`` fall back to ``hwnd`` -- exactly right here.
"""

import re
import time

import psutil

from ok.compat import x11
from ok.util.logger import Logger

logger = Logger.get_logger("capture")

# `find_hwnd` runs on the 0.2s poll thread. When the game is not running it matches
# nothing on every single call, so its "why did nothing match" report is rate-limited to
# this interval, and reset as soon as something matches.
_NO_MATCH_LOG_INTERVAL = 30
_last_no_match_log = 0

# Wine and Steam helper executables that appear in a game's command line but are never the
# game. Only used to pick the *primary* exe of a process; matching against a caller's
# `exe_names` still considers every candidate, so a caller that really wants one of these
# can still find it.
_HELPER_EXES = frozenset((
    'wine', 'wine64', 'wine-preloader', 'wine64-preloader', 'wineserver',
    'wineboot.exe', 'winedevice.exe', 'winemenubuilder.exe', 'explorer.exe',
    'services.exe', 'plugplay.exe', 'rpcss.exe', 'svchost.exe', 'conhost.exe',
    'start.exe', 'cmd.exe', 'tabtip.exe', 'steam.exe', 'gameoverlayui.exe',
    'steamwebhelper.exe', 'proton', 'python.exe',
))


def _basename(path):
    """Basename for a path that may use either separator -- Windows paths arrive via Wine."""
    return re.split(r'[\\/]', path)[-1]


def _exe_candidates(pid):
    """Every executable name this pid could reasonably be identified by, best first.

    A Proton game is a Linux process whose ``/proc/<pid>/exe`` is a Wine loader; the name
    ok-ww matches on (``Client-Win64-Shipping.exe``) appears only in the command line. So
    the Windows executables named on the command line come first, the Wine/Steam helpers
    among them last, and the Linux process itself is the final fallback -- which is also
    what makes native Linux windows resolve sensibly for the device picker.

    Returns a list of ``(name, full_path)``.
    """
    cmdline = []
    linux_name = ''
    linux_path = ''
    try:
        process = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return [], []
    for getter, target in ((process.cmdline, 'cmdline'), (process.name, 'name'), (process.exe, 'exe')):
        try:
            value = getter()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError) as e:
            logger.debug(f'_exe_candidates process.{target}() failed for {pid}: {e}')
            continue
        if target == 'cmdline':
            cmdline = value or []
        elif target == 'name':
            linux_name = value or ''
        else:
            linux_path = value or ''

    windows_exes, helpers = [], []
    for arg in cmdline:
        if not isinstance(arg, str) or not arg.lower().endswith('.exe'):
            continue
        name = _basename(arg)
        entry = (name, arg)
        if entry in windows_exes or entry in helpers:
            continue
        (helpers if name.lower() in _HELPER_EXES else windows_exes).append(entry)

    candidates = windows_exes + helpers
    if linux_name:
        candidates.append((linux_name, linux_path))
    return candidates, cmdline


def get_exe_by_hwnd(hwnd):
    """``(name, full_path, cmdline)`` for the process owning an X11 window.

    Mirrors the Windows function's shape, including its ``("", "", "")`` on any failure.
    ``name`` is the game's ``.exe`` when there is one, not the Wine loader.
    """
    try:
        pid = x11.get_pid(hwnd)
        if pid <= 0:
            return "", "", ""
        candidates, cmdline = _exe_candidates(pid)
        if not candidates:
            return "", "", cmdline or ""
        name, full_path = candidates[0]
        return name, full_path, cmdline
    except Exception as e:
        logger.error('get_exe_by_hwnd error', e)
        return "", "", ""


def _match_exe_names(candidates, exe_names):
    """The ``(name, full_path)`` a caller's ``exe_names`` selects, or ``None``.

    Same comparison as upstream (``compare_path_safe``: separator- and case-insensitive),
    applied to every candidate rather than only the primary one, because under Wine the
    game's name can sit anywhere in the command line.
    """
    from ok.util.window import compare_path_safe
    for name, full_path in candidates:
        for exe_name in exe_names:
            if compare_path_safe(name, exe_name) or compare_path_safe(exe_name, full_path):
                return name, full_path
    return None


def is_window_minimized(hwnd):
    return x11.is_minimized(hwnd)


def is_foreground_window(hwnd):
    """True when the window holds input focus.

    This is the predicate upstream calls ``visible`` [V15] -- a *focus* test, not a
    mapped test. Getting it wrong inverts ``MouseResetTask``'s cursor pinning, which is
    the one thing background play depends on.
    """
    return x11.is_active(hwnd)


def get_window_bounds(hwnd):
    """``(x, y, window_width, window_height, width, height, scaling)``.

    ``x, y`` and ``width, height`` describe the client area -- the X client window *is* the
    client area, the decorations belong to the WM's frame -- so they are the direct
    analogue of ``ClientToScreen`` + ``GetClientRect``. ``window_*`` adds the frame back on
    via ``_NET_FRAME_EXTENTS``, which is what upstream's ``GetWindowRect`` measures and what
    ``try_resize_to`` subtracts to derive the border and title-bar height.

    Returns upstream's ``(0, 0, 0, 0, 0, 0, 1)`` on any failure; callers rely on that shape.
    """
    try:
        geometry = x11.get_abs_geometry(hwnd)
        if geometry is None:
            return 0, 0, 0, 0, 0, 0, 1
        x, y, width, height = geometry
        left, right, top, bottom = x11.get_frame_extents(hwnd)
        # Xwayland reports device pixels, so there is no separate DPI scale to divide out.
        # Fractional desktop scaling would need a per-monitor factor here; see Phase 2.
        return x, y, width + left + right, height + top + bottom, width, height, 1.0
    except Exception as e:
        logger.error('get_window_bounds exception', e)
        return 0, 0, 0, 0, 0, 0, 1


def show_title_bar(hwnd):
    """No-op on Linux: decorations are the window manager's, not the client's.

    Upstream flips ``WS_CAPTION`` on so that ``try_resize_to`` can measure the title-bar
    height. Under Wine the game's Win32 style is invisible from X11, and the WM already
    reports the frame through ``_NET_FRAME_EXTENTS``. Returning True keeps
    ``try_resize_to`` on its normal path.
    """
    logger.debug(f'show_title_bar is a no-op on Linux for {hwnd}')
    return True


def resize_window(hwnd, width, height):
    """Resize the **window** rect to ``width x height`` and centre it on its monitor.

    ``width``/``height`` are outer dimensions -- decorations included -- because that is
    what the Windows body means: it calls ``SetWindowPos``, which sizes the window rect,
    and settles against ``GetWindowRect``. Both callers pass outer dimensions:
    ``try_resize_to`` adds the border and title-bar height it measured to the target
    resolution, and ``start_controller``'s re-centre path passes ``window_width`` /
    ``window_height`` straight through. X11 has no window rect -- the client window is the
    client area and the frame belongs to the WM -- so the frame extents come off here, and
    go back on for the settle check.

    Sizing the *client* to these numbers instead (which is what this did before) is wrong
    twice over: ``try_resize_to``'s content ends up one title bar too tall and its success
    test then fails despite the WM having obeyed, and the re-centre path grows the window
    by the frame extents on every call, without bound.

    Same contract as the Windows version otherwise, including the up-to-5-second settle
    wait: a WM resize is a request, and the answer arrives asynchronously. False when the
    window refuses to reach the requested size -- under Proton the game usually owns its
    own resolution, and upstream's caller treats failure as non-fatal.
    """
    if not hwnd:
        logger.info("Invalid window handle provided.")
        return False
    try:
        left, right, top, bottom = x11.get_frame_extents(hwnd)
        # Undecorated (every extent 0) is the byte-identical no-op of the old behaviour.
        client_width = max(1, width - left - right)
        client_height = max(1, height - top - bottom)
        geometry = x11.get_abs_geometry(hwnd) or (0, 0, client_width, client_height)
        bounds = x11.monitor_for(*geometry)
        if bounds:
            monitor_left, monitor_top, monitor_right, monitor_bottom = bounds
            # Centre the *window* rect, and pass its top-left. A reparenting WM applies
            # ICCCM win_gravity to a ConfigureRequest, so with the default NorthWest
            # gravity these coordinates place the frame, not the client -- which is
            # exactly what centring outer dimensions wants. Verified against KWin.
            center_x = monitor_left + (monitor_right - monitor_left - width) // 2
            center_y = monitor_top + (monitor_bottom - monitor_top - height) // 2
        else:
            center_x, center_y = 0, 0
        if not x11.resize(hwnd, client_width, client_height, center_x, center_y):
            return False

        start_time = time.time()
        while time.time() - start_time < 5:
            geometry = x11.get_abs_geometry(hwnd)
            extents = x11.get_frame_extents(hwnd)
            if geometry and (geometry[2] + extents[0] + extents[1] == width
                             and geometry[3] + extents[2] + extents[3] == height):
                break
            time.sleep(0.1)
        else:
            logger.error(f'resize_window {hwnd} did not settle at {width}x{height} '
                         f'(client target {client_width}x{client_height}, frame {left},{right},{top},{bottom})')
            return False

        time.sleep(0.5)
        logger.info(f"Window with handle {hwnd} resized to {width}x{height} and centered at ({center_x}, {center_y}).")
        return True
    except Exception as e:
        logger.error(f"Error resizing and centering window with handle {hwnd}: {e}")
        return False


def find_all_visible_windows():
    """``(hwnd, title, exe_name, exe_full_path)`` per titled, mapped toplevel.

    Feeds ``DeviceManager.update_pc_device``'s "let the user pick a window" path, which is
    only taken when the app declares no exe/class/title of its own.
    """
    windows = []
    for hwnd in x11.list_clients():
        if not x11.is_viewable(hwnd):
            continue
        title = x11.get_name(hwnd)
        if not title or not title.strip():
            continue
        exe_name, exe_full_path, _ = get_exe_by_hwnd(hwnd)
        windows.append((hwnd, title, exe_name, exe_full_path))
    return windows


def find_hwnd(title, exe_names, frame_width, frame_height, player_id=-1, class_name=None,
              selected_hwnd=0, top_hwnd_class=None, last_hwnd=0):
    """The Linux ``find_hwnd``. Return shape is byte-identical to the Windows one:
    ``(name, hwnd, full_path, real_x_offset, real_y_offset, real_width, real_height, hwnds)``.

    ``real_width``/``real_height`` are the matched **window's** size, never 0 [V18]:
    ``DeviceManager`` writes them straight into the PC device dict and
    ``capture_target_signature`` changes with them, so zeros give a ``0x0`` device and kill
    change-detection. Only the two offsets are 0 -- Wine gives one X toplevel per game
    [V11], so there is no letterboxed child window to find and no ``enum_child_windows``
    equivalent.

    ``class_name``/``top_hwnd_class`` are accepted and ignored; see the module docstring.
    """
    if exe_names is None and title is None:
        return None, 0, None, 0, 0, 0, 0, []
    if isinstance(exe_names, str):
        exe_names = [exe_names]
    if class_name is not None or top_hwnd_class is not None:
        logger.debug(f'find_hwnd ignoring Win32 class filters on Linux: {class_name} {top_hwnd_class}')

    from ok.util.window import get_player_id_from_cmdline

    results = []
    # Why a window was rejected is the whole diagnostic value of this function when it
    # matches nothing -- "game not running" and "the game is running but its _NET_WM_PID
    # is not a pid we can see" are the same empty tuple otherwise, and the second is
    # exactly what a pressure-vessel PID namespace would look like [GATE-1b]. Collected
    # rather than logged inline, and reported at most once every `_NO_MATCH_LOG_INTERVAL`
    # seconds: this runs on the 0.2s poll thread for the whole time the game is not
    # running, and five identical lines a second is not diagnosis, it is noise.
    rejects = []
    toplevels = 0
    for hwnd in x11.list_clients():
        state = x11.get_wm_state(hwnd)
        if state == x11.WITHDRAWN_STATE:
            continue
        toplevels += 1

        text = x11.get_name(hwnd)
        if title:
            if isinstance(title, str):
                if title != text:
                    continue
            elif not re.search(title, text):
                continue

        pid = x11.get_pid(hwnd)
        if pid <= 0:
            rejects.append(f'{hwnd} ({text!r}): no _NET_WM_PID')
            continue
        candidates, cmdline = _exe_candidates(pid)
        if not candidates and not cmdline:
            rejects.append(f'{hwnd} ({text!r}): pid {pid} is not resolvable in /proc')

        if exe_names:
            matched = _match_exe_names(candidates, exe_names)
            if matched is None:
                rejects.append(f'{hwnd} ({text!r}): pid {pid} {[c[0] for c in candidates]} '
                               f'does not match {exe_names}')
                continue
            name, full_path = matched
        elif candidates:
            name, full_path = candidates[0]
        else:
            name, full_path = "", ""

        if player_id != -1 and player_id != get_player_id_from_cmdline(cmdline):
            logger.warning(
                f'player id check failed,cmdline {cmdline} {get_player_id_from_cmdline(cmdline)} != {player_id}')
            continue

        geometry = x11.get_abs_geometry(hwnd)
        if geometry is None:
            continue
        x, y, width, height = geometry
        # Same >10px floor as upstream. It is also what discards Wine's 1x1 "Default IME"
        # helper toplevels, which share the game's pid and would otherwise tie on it.
        if width <= 10 or height <= 10:
            rejects.append(f'{hwnd} ({text!r}): {width}x{height} is at or below the 10px floor')
            continue
        results.append((hwnd, full_path, width, height, x, y, text, '', 1.0))

    if not results:
        global _last_no_match_log
        now = time.time()
        if toplevels and now - _last_no_match_log > _NO_MATCH_LOG_INTERVAL:
            _last_no_match_log = now
            logger.info(f'find_hwnd matched none of {toplevels} toplevel windows '
                        f'(title={title!r} exe_names={exe_names} player_id={player_id}): '
                        + '; '.join(rejects))
        return None, 0, None, 0, 0, 0, 0, []
    _last_no_match_log = 0

    w_biggest = max(results, key=lambda r: r[2] * r[3])
    w_selected = next((r for r in results if 0 < selected_hwnd == r[0]), None)
    w_last = next((r for r in results if 0 < last_hwnd == r[0]), None)

    biggest = w_biggest
    if w_selected:
        biggest = w_selected
    elif w_last and w_biggest:
        if (w_biggest[2] * w_biggest[3]) <= (w_last[2] * w_last[3]) * 1.1:
            biggest = w_last

    return biggest[6], biggest[0], biggest[1], 0, 0, biggest[2], biggest[3], []
