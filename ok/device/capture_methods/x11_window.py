"""``X11Window`` -- the Linux ``HwndWindow``.

`ok/device/capture_methods/__init__.py` rebinds ``HwndWindow`` to this class on Linux, so
``DeviceManager`` constructs it without knowing the difference. It subclasses
``HwndWindow`` and overrides exactly the methods that call into Win32; the eleven pure
ones (``get_abs_cords``, ``get_capture_origin``, ``get_top_window_cords``,
``capture_target_signature``, ``update_window``, ``update_frame_size``, ``frame_ratio``,
``stop``, ``_front_hwnd_candidates``, ``_top_hwnd_info``, ``__str__``) are inherited
unchanged, so upstream keeps owning them across rebases.

``__init__`` is copied rather than inherited because upstream's calls
``get_monitors_bounds()`` from *its* module globals, which is Win32.
``tests/test_x11_window.py`` carries a drift gate over that copy: it walks both classes'
ASTs and fails if upstream grows an attribute or a method this class does not cover.

Three semantics are easy to get wrong and are load-bearing:

* **``visible`` means FOREGROUND, not mapped.** Upstream sets ``visible =
  self.is_foreground()`` and ``MouseResetTask`` pins the physical cursor only while
  ``not hwnd.visible`` -- i.e. exactly during background play, which is what this port
  exists for. A mapped-based ``visible`` is True for the whole session and silently
  disables it.
* **Iconic is a different signal, and ``check_pos`` will not catch it.** An iconified X11
  window keeps its last geometry, so ``check_pos`` alone stays True forever, the executor
  is never paused, and capture throws in a loop with no explanation. ``pos_valid`` gets an
  explicit minimized test.
* **``hwnd`` is an X11 window id**, a plain int, so every ``if hwnd > 0`` in upstream keeps
  working. There is one X toplevel per Wine game, so ``top_hwnd == hwnd``, the top offsets
  are 0, and ``hwnds`` is empty.
"""

import shutil
import subprocess
import threading
import time

import psutil

from ok.compat import x11
from ok.core.events import communicate
from ok.core.notifications import alert_info
from ok.util.GlobalConfig import basic_options
from ok.util.logger import Logger
from ok.util.window import get_window_bounds, is_foreground_window, find_hwnd, resize_window, show_title_bar

from ok.device.capture_methods.base import BaseWindowsCaptureMethod
# check_pos and is_window_in_screen_bounds are pure arithmetic on rectangles; reuse them so
# the 20-pixel tolerance and the >= 0 rule stay in one place. They are re-exported because
# `capture_methods/__init__.py` shadows the whole five-name group from this module.
from ok.device.capture_methods.hwnd_window import HwndWindow, check_pos, is_window_in_screen_bounds

logger = Logger.get_logger(__name__)

_pactl_missing_logged = False


def get_monitors_bounds():
    """Monitor rectangles as ``(left, top, right, bottom)``, like ``EnumDisplayMonitors``.

    RandR via python-xlib. Feeds ``is_window_in_screen_bounds``, which is why the shape has
    to be right-bottom rather than width-height.
    """
    return x11.get_monitors()


def _pactl(*args):
    """Run ``pactl`` and return stdout, or ``None`` when it is absent or fails."""
    global _pactl_missing_logged
    if shutil.which('pactl') is None:
        if not _pactl_missing_logged:
            _pactl_missing_logged = True
            logger.warning('pactl not found, per-application mute is unavailable on this system')
        return None
    try:
        result = subprocess.run(('pactl',) + args, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f'pactl {" ".join(args)} failed: {e}')
        return None
    if result.returncode != 0:
        logger.warning(f'pactl {" ".join(args)} exited {result.returncode}: {result.stderr.strip()}')
        return None
    return result.stdout


def _parse_sink_inputs(text):
    """``pactl list sink-inputs`` -> ``[(sink_input_id, process_pid, muted)]``.

    The text format is parsed rather than ``-f json`` because the JSON formatter is a
    recent addition and this has to work on the distro pulseaudio too.
    """
    entries = []
    index = None
    pid = 0
    muted = False
    for raw in (text or '').splitlines():
        line = raw.strip()
        if line.startswith('Sink Input #'):
            if index is not None:
                entries.append((index, pid, muted))
            index, pid, muted = line[len('Sink Input #'):].strip(), 0, False
            continue
        if index is None:
            continue
        if line.startswith('Mute:'):
            muted = line.split(':', 1)[1].strip().lower() in ('yes', 'true', '1')
        elif line.startswith('application.process.id'):
            value = line.split('=', 1)[-1].strip().strip('"')
            pid = int(value) if value.isdigit() else 0
    if index is not None:
        entries.append((index, pid, muted))
    return entries


def _sink_inputs_for_hwnd(hwnd):
    """The ``(sink_input_id, muted)`` streams belonging to the window's process.

    The game under Proton owns a PipeWire/PulseAudio *sink-input*, keyed by the Linux pid
    that ``_NET_WM_PID`` already gave us. Descendants are checked as a fallback because a
    Proton game can open its audio from a helper process rather than the one that owns the
    window.
    """
    pid = x11.get_pid(hwnd)
    if pid <= 0:
        return []
    entries = _parse_sink_inputs(_pactl('list', 'sink-inputs'))
    if not entries:
        return []
    matched = [(index, muted) for index, entry_pid, muted in entries if entry_pid == pid]
    if matched:
        return matched
    try:
        descendants = {child.pid for child in psutil.Process(pid).children(recursive=True)}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    return [(index, muted) for index, entry_pid, muted in entries if entry_pid in descendants]


def get_mute_state(hwnd):
    """1 when any of the window's audio streams is muted, else 0. Never raises."""
    try:
        streams = _sink_inputs_for_hwnd(hwnd)
        return 1 if any(muted for _, muted in streams) else 0
    except Exception as e:
        logger.warning(f"get_mute_state exception: {e}")
        return 0


def set_mute_state(hwnd, mute):
    """Mute (1) or unmute (0) every audio stream of the window's process. Never raises."""
    try:
        for index, _ in _sink_inputs_for_hwnd(hwnd):
            _pactl('set-sink-input-mute', str(index), '1' if mute else '0')
    except Exception as e:
        logger.warning(f"No audio stream for this window, skip mute. Exception: {e}")


class X11Window(HwndWindow):

    def __init__(self, exit_event, title, exe_name=None, frame_width=0, frame_height=0, player_id=-1, hwnd_class=None,
                 global_config=None, device_manager=None, top_hwnd_class=None):
        # Not `super().__init__(...)`: upstream's body calls the Win32 `get_monitors_bounds`
        # out of its own module globals. Everything else here is upstream's attribute list,
        # verbatim and in order -- `tests/test_x11_window.py` fails if the two drift apart.
        logger.info(
            f'X11Window init title:{title} player_id:{player_id} exe_name:{exe_name} hwnd_class:{hwnd_class} top_hwnd_class:{top_hwnd_class}')
        self.app_exit_event = exit_event
        self.exe_names = None
        self.visible_monitors = []
        self.device_manager = device_manager
        self.to_handle_mute = True
        self.title = title
        self.stop_event = threading.Event()
        self.visible = False
        self.player_id = player_id
        self.window_width = 0
        self.window_height = 0
        self.client_width = 0
        self.client_height = 0
        self.x = 0
        self.y = 0
        self.width = 0
        self.height = 0
        self.hwnd = 0
        self.frame_width = 0
        self.frame_height = 0
        self.exists = False
        self.title = None
        self.exe_full_path = None
        self.real_width = 0
        self.real_height = 0
        self.real_x_offset = 0
        self.real_y_offset = 0
        self.scaling = 1.0
        self.frame_aspect_ratio = 0
        self.last_mute_check = 0
        self.hwnds = []
        self.top_hwnd = 0
        self.top_offset_x = 0
        self.top_offset_y = 0

        self.hwnd_class = hwnd_class
        self.top_hwnd_class = top_hwnd_class
        self.pos_valid = False
        self._hwnd_title = ""
        self.monitors_bounds = get_monitors_bounds()
        self.mute_option = global_config.get_config(basic_options)
        self.global_config = global_config
        self.mute_option.validator = self.validate_mute_config
        self.update_window(title, exe_name, frame_width, frame_height, player_id, hwnd_class, top_hwnd_class)
        self.thread = threading.Thread(target=self.update_window_size, name="update_window_size", daemon=True)
        self.thread.start()

    def validate_mute_config(self, key, value):
        if key == 'Mute Game while in Background' and self.hwnd:
            logger.info(f'validate_mute_config {value}')
            if value:
                self.handle_mute(value)
            else:
                logger.info(f'config changed unmute set_mute_state {value}')
                set_mute_state(self.hwnd, 0)
        return True, None

    def update_window_size(self):
        # 0.2s exactly, as upstream: change detection and the 2-second `last_mute_check`
        # interval below are tuned to this cadence.
        try:
            while not self.app_exit_event.is_set() and not self.stop_event.is_set():
                self.do_update_window_size()
                time.sleep(0.2)
            if self.hwnd and self.mute_option.get('Mute Game while in Background'):
                logger.info(f'exit reset mute state to 0')
                set_mute_state(self.hwnd, 0)
        except Exception as error:
            logger.error(f'update_window_size exception: {error}')

    def handle_mute(self, mute=None):
        if mute is None:
            mute = self.mute_option.get('Mute Game while in Background')
        if self.hwnd and self.to_handle_mute and mute:
            set_mute_state(self.hwnd, 0 if self.visible else 1)

    def is_foreground(self):
        if is_foreground_window(self.hwnd):
            return True
        for w in self.hwnds:
            if is_foreground_window(w[0]):
                return True
        return False

    def is_minimized(self):
        """Iconified, hidden or unmapped -- the state ``pos_valid`` must veto.

        Not the same question as ``is_foreground()``: a backgrounded game is still mapped
        and still capturable, which is the entire point of the port.
        """
        return bool(self.hwnd) and x11.is_minimized(self.hwnd)

    def bring_to_front(self):
        # Same contract as upstream: True/False, one retry after a forced refresh, and it
        # must never raise. A compositor with focus-stealing prevention (KWin and Mutter
        # both, by default) is entitled to refuse, and that is not an error here.
        errors = []
        for refreshed in (False, True):
            hwnds = self._front_hwnd_candidates()
            if not hwnds:
                if not refreshed:
                    self.do_update_window_size()
                    continue
                logger.warning('bring_to_front failed: no hwnd found')
                return False

            invalid_hwnds = []
            for hwnd in hwnds:
                if not x11.exists(hwnd):
                    invalid_hwnds.append(hwnd)
                    continue
                if x11.activate(hwnd):
                    return True
                errors.append(f'{hwnd}: the window manager refused _NET_ACTIVE_WINDOW')

            if invalid_hwnds and len(invalid_hwnds) == len(hwnds) and not refreshed:
                self.do_update_window_size()
                continue
            if invalid_hwnds:
                errors.append(f'invalid hwnds: {invalid_hwnds}')
            break

        logger.warning(f'bring_to_front failed: {", ".join(errors)}')
        return False

    def try_resize_to(self, resize_to):
        if not self.global_config.get_config('Basic Options').get('Auto Resize Game Window'):
            return False
        if self.hwnd and self.window_width > 0:
            show_title_bar(self.hwnd)
            # Upstream reads the primary screen via GetSystemMetrics(0/1). Take the monitor
            # the window is actually on instead -- multi-head is the norm here and the
            # RandR list is already built for `monitors_bounds`.
            monitor = x11.monitor_for(self.x, self.y, self.width, self.height)
            if monitor:
                screen_width = monitor[2] - monitor[0]
                screen_height = monitor[3] - monitor[1]
            else:
                logger.error('try_resize_to found no monitor for the window')
                return False
            x, y, window_width, window_height, width, height, scaling = get_window_bounds(self.hwnd)
            title_height = window_height - height
            logger.info(f'try_resize_to {x, y, window_width, window_height, width, height, scaling} ')
            border = window_width - width
            resize_width = 0
            resize_height = 0
            for resolution in resize_to:
                if screen_width >= border + resolution[0] and screen_height >= title_height + resolution[
                    1]:
                    resize_width = resolution[0] + border
                    resize_height = resolution[1] + title_height
                    break
            if resize_width > 0:
                resize_window(self.hwnd, resize_width, resize_height)
                self.do_update_window_size()
                if self.window_height == resize_height and self.window_width == resize_width:
                    logger.info(f'resize hwnd success to {self.width}x{self.height}')
                    return True
                else:
                    # Under Proton the game usually owns its own resolution; upstream's
                    # caller treats this as non-fatal and `supported_resolution` handles it.
                    logger.error(f'resize hwnd failed: {self.width}x{self.height}')

    def do_update_window_size(self):
        if self.device_manager and getattr(self.device_manager, 'capture_method', None):
            from ok.device.capture_methods.browser import BrowserCaptureMethod
            if isinstance(self.device_manager.capture_method, BrowserCaptureMethod):
                return
        try:
            changed = False
            exists = False
            visible, x, y = self.visible, self.x, self.y
            window_width, window_height = self.window_width, self.window_height
            client_width, client_height = self.client_width, self.client_height
            width, height, scaling = self.width, self.height, self.scaling
            name, find_hwnd_res, exe_full_path, real_x_offset, real_y_offset, real_width, real_height, hwnds = find_hwnd(
                self.title,
                self.exe_names or self.device_manager.config.get('selected_exe'),
                self.frame_width, self.frame_height, player_id=self.player_id, class_name=self.hwnd_class,
                selected_hwnd=self.device_manager.config.get('selected_hwnd'),
                top_hwnd_class=self.top_hwnd_class, last_hwnd=self.hwnd)

            if find_hwnd_res > 0 and self.hwnd != find_hwnd_res:
                old_hwnd = self.hwnd
                self.hwnd = find_hwnd_res
                self.exe_full_path = exe_full_path
                self._hwnd_title = ""
                # No GetClassName here: the Win32 class is invisible from X11 [V11].
                logger.info(
                    f'do_update_window_size hwnd changed from {old_hwnd} to {self.hwnd} top {hwnds[0][0] if hwnds else self.hwnd} {self.exe_full_path} real:{real_x_offset},{real_y_offset},{real_width},{real_height}')
                changed = True

            if find_hwnd_res > 0:
                # `hwnds` is always empty on Linux -- one X toplevel per Wine game -- so
                # `_top_hwnd_info` returns None and top_hwnd/top_offset_* collapse onto the
                # main window, which is what upstream's own no-child branch does anyway.
                self.hwnds = hwnds
                self.real_x_offset = real_x_offset
                self.real_y_offset = real_y_offset
                self.real_width = real_width
                self.real_height = real_height
                top_hwnd_info = self._top_hwnd_info(hwnds)
                self.top_hwnd = top_hwnd_info[0] if top_hwnd_info else self.hwnd
                self.top_offset_x = 0
                self.top_offset_y = 0

            exists = self.hwnd > 0
            if self.hwnd > 0:
                exists = x11.exists(self.hwnd)
                if exists:
                    visible = self.is_foreground()
                    x, y, window_width, window_height, width, height, scaling = get_window_bounds(
                        self.hwnd)
                    client_width, client_height = width, height
                    if self.frame_aspect_ratio != 0 and height != 0:
                        window_ratio = width / height
                        if window_ratio < self.frame_aspect_ratio:
                            cropped_window_height = int(width / self.frame_aspect_ratio)
                            height = cropped_window_height
                    # An iconified X11 window keeps its last geometry, so check_pos alone
                    # would stay True forever and the executor would never pause.
                    pos_valid = (not self.is_minimized()) and check_pos(x, y, width, height, self.monitors_bounds)
                    if isinstance(self.device_manager.capture_method,
                                  BaseWindowsCaptureMethod) and not pos_valid and pos_valid != self.pos_valid and self.device_manager.executor is not None:
                        if self.device_manager.executor.pause():
                            logger.error(f'og.executor.pause pos_invalid: {x, y, width, height}')
                            communicate.notification.emit('Paused because game window is minimized or out of screen!',
                                                          None,
                                                          True, True, "start", None, None)
                    if pos_valid != self.pos_valid:
                        self.pos_valid = pos_valid
                else:
                    if self.global_config.get_config('Basic Options').get(
                            'Exit App when Game Exits') and self.device_manager.executor is not None and self.device_manager.executor.pause():
                        alert_info('Auto exit because game exited', True)
                        communicate.quit.emit()
                    else:
                        communicate.notification.emit('Game Exited', None, True, True, None, None, None)
                    self.hwnd = 0
                    visible = False
                if visible != self.visible:
                    self.visible = visible
                    for visible_monitor in self.visible_monitors:
                        visible_monitor.on_visible(visible)
                    changed = True

                if changed or (time.time() - self.last_mute_check > 2):
                    self.handle_mute()
                    self.last_mute_check = time.time()

                if (window_width != self.window_width or window_height != self.window_height or
                    client_width != self.client_width or client_height != self.client_height or
                    x != self.x or y != self.y or width != self.width or height != self.height or scaling != self.scaling) and (
                        (x >= -1 and y >= -1) or self.visible):
                    self.x, self.y = x, y
                    self.window_width, self.window_height = window_width, window_height
                    self.client_width, self.client_height = client_width, client_height
                    self.width, self.height, self.scaling = width, height, scaling
                    changed = True
                if self.exists != exists:
                    self.exists = exists
                    changed = True
                if changed:
                    device = self.device_manager.get_preferred_device()
                    if device:
                        logger.info(f"hwnd changed,connected:{self.exists}")
                        device['connected'] = self.exists
                        device['width'] = width
                        device['height'] = height
                        device['resolution'] = f"{width}x{height}"
                        communicate.adb_devices.emit(True)
                    logger.info(
                        f"do_update_window_size changed,visible:{self.visible},exists:{self.exists} x:{self.x} y:{self.y} window:{self.width}x{self.height} self.window:{self.window_width}x{self.window_height} real:{self.real_width}x{self.real_height}")
                    capture_x, capture_y = self.get_capture_origin()
                    communicate.window.emit(self.visible, capture_x, capture_y,
                                            self.window_width, self.window_height,
                                            self.width,
                                            self.height, self.scaling)
        except Exception as e:
            logger.error(f"do_update_window_size exception", e)

    @property
    def hwnd_title(self):
        if not self._hwnd_title:
            if self.hwnd:
                self._hwnd_title = x11.get_name(self.hwnd)
        return self._hwnd_title
