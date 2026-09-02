"""``WinePostMessageInteraction`` -- the Linux input backend (PORT.md Phase 4c).

``PostMessageInteraction``, method for method, with every ``self.post(...)`` replaced by a
line written to ``okww-input-shim.exe`` running inside the game's Proton prefix. The shim
does the actual ``PostMessageW``, which is the only way to reach an *unfocused* window
[PORT.md V6]: from Linux, ``XSendEvent`` is focus-bound [V5] and XTEST is global, so
neither can play in the background.

``ok/compat/proton_shim.py`` owns finding the prefix, launching the shim and the socket;
this module owns the semantics ok-ww's task code depends on. Five of those are easy to get
wrong:

* **The hot path is fire-and-forget.** Upstream's ``post()`` (``post_message.py:91-97``)
  swallows every exception and returns nothing, so no caller ever reads a result. Keys,
  characters, mouse moves, buttons, wheel, activate and setcursor are therefore written and
  not waited on -- otherwise every combat keypress, and every one of ``swipe``'s hundred
  ``move()`` calls, would pay a round-trip. Only ``FINDWIN``/``GEOM``/``GETCURSOR``/
  ``VKKEYSCAN``/``PING`` are request-response, and the shim replies to nothing else.
* **A dropped command is logged and counted, never raised.** Same reason: upstream cannot
  fail a keypress, and a task that suddenly saw exceptions from ``send_key`` would abort
  runs whenever the shim restarted.
* **``update_mouse_pos`` is the identity here, minus the ``-1`` branch.** Upstream converts
  client -> screen -> client through a child window picked by hit test; on Linux
  ``hwnds`` is empty and ``top_hwnd == hwnd`` [Phase 2], so the two conversions cancel.
  The ``-1`` branch is *not* optional -- ``click(-1, -1)``, ``right_click(-1, -1)`` and
  ``mouse_down(-1, -1)`` all take it, and it must reuse the cached position without
  overwriting it.
* **Two upstream bugs are fixed here and left alone on Windows** (PORT.md §4c): ``swipe``'s
  ``steps = int(duration / 100)`` is 0 at the default ``duration=3`` and divides by zero,
  and ``mouse_up`` releases at ``self.mouse_pos``, which is set to ``(0, 0)`` once and
  never written again -- so upstream ends every drag at client ``(0, 0)``.
* **``try_activate`` is called from exactly the places upstream calls it** -- ``send_key_down``,
  ``input_text``, ``scroll``, ``update_mouse_pos``. Skipping it causes intermittent input
  loss. It costs one fire-and-forget write, and one rather than upstream's two, because on
  Linux ``hwnd`` and ``hwnd_window.hwnd`` are the same window.

The connection is maintained by a background thread, so a game that is not running yet, a
prefix that is still upgrading, or a shim that died costs the task thread nothing: sends
are dropped while the link is down and resume when it comes back.
"""

import threading
import time

from ok.compat.proton_shim import (
    ShimError, WUWA_EXE, connect_or_start, game_pid, resolve_steam_game, steam_appid,
)
from ok.device.capture_methods.base import BaseCaptureMethod
from ok.device.interaction_methods.base import BaseInteraction
from ok.device.interaction_methods.keys import vk_key_dict
from ok.util.logger import Logger

logger = Logger.get_logger(__name__)

# Button masks, as `win32con` names them. The shim builds the wparam; these are only used
# for `swipe`, which drags with the left button held.
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010

# How long a `get_cursor_pos()` answer is reused. `MouseResetTask` polls every 2 ms
# (`MouseResetTask.py:57`) and only ever asks whether the cursor jumped more than 200 px,
# which tolerates far more staleness than this; without the cache that poll alone would
# saturate the link.
CURSOR_CACHE_SECONDS = 0.05

# Backoff between connection attempts, in seconds. The first failure is usually "the game
# is not running yet", which is a normal state that can last for minutes.
RECONNECT_BACKOFF = (2, 5, 10, 30)

DROP_LOG_INTERVAL = 5.0


class WinePostMessageInteraction(BaseInteraction):

    def __init__(self, capture: BaseCaptureMethod, hwnd_window):
        super().__init__(capture)
        self.hwnd_window = hwnd_window
        # Upstream keeps `mouse_pos` too, and never writes it; the release coordinate here
        # comes from `bg_mouse_pos`, which `update_mouse_pos` actually maintains.
        self.bg_mouse_pos = (0, 0)
        self.lparam = 0x1e0001

        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._client = None
        self._process = None
        self._game = None
        self._connect_thread = None
        self._closing = False
        self._failures = 0
        self._last_error = None
        self._vk_cache = {}
        self._cursor_pos = (0, 0)
        self._cursor_time = 0.0
        self._dropped = 0
        self._last_drop_log = 0.0
        self._wake = threading.Event()
        self._ensure_connection()

    # ------------------------------------------------------------- connection ----

    @property
    def connected(self):
        return self._client is not None

    def _ensure_connection(self):
        """Make sure the maintainer thread is running. Cheap enough to call per send."""
        with self._state_lock:
            if self._closing:
                return
            if self._connect_thread is not None and self._connect_thread.is_alive():
                return
            self._connect_thread = threading.Thread(target=self._maintain,
                                                    name='okww_shim_connect', daemon=True)
            self._connect_thread.start()

    def _maintain(self):
        """Keep a connection up for as long as this backend lives.

        A thread rather than an attempt-per-send, because the usual first answer is "the
        game is not running yet": ok-ww is normally started *before* the game, and a
        connection that only retried when something was sent would drop every key of the
        first few seconds of play while the launch took its 10-20 s.
        """
        while not self._closing:
            if self._client is None:
                self._connect()
            self._wake.wait(self._sleep_seconds())
            self._wake.clear()

    def _sleep_seconds(self):
        if self._client is not None:
            return 5.0
        return RECONNECT_BACKOFF[min(max(self._failures, 1), len(RECONNECT_BACKOFF)) - 1]

    def _connect(self):
        try:
            game = self._game or resolve_steam_game(appid=steam_appid())
            self._game = game
            exe_name = self._target_exe()
            if game_pid(exe_name) is None:
                raise ShimError(f'{exe_name} is not running; launch the game through Steam')
            client, process = connect_or_start(game, exe_name=exe_name,
                                               hwnd_class=self.hwnd_window.hwnd_class)
            with self._state_lock:
                if self._closing:
                    client.close()
                    return
                self._client = client
                self._process = process
                self._failures = 0
                self._last_error = None
            logger.info(f'input shim connected, game window {client.hwnd}')
        except (ShimError, OSError) as e:
            with self._state_lock:
                self._failures += 1
                first_time = str(e) != self._last_error
                self._last_error = str(e)
            # Only the first occurrence of each distinct message is an error: "the game is
            # not running" repeated every two seconds is noise, not a fault.
            if first_time:
                logger.error(f'input shim unavailable: {e}')
            else:
                logger.debug(f'input shim still unavailable: {e}')

    def _target_exe(self):
        names = getattr(self.hwnd_window, 'exe_names', None) or []
        return names[0] if names else WUWA_EXE

    def _drop(self, line, error=None):
        self._dropped += 1
        now = time.monotonic()
        if now - self._last_drop_log >= DROP_LOG_INTERVAL:
            self._last_drop_log = now
            logger.error(f'input dropped ({self._dropped} so far), shim not reachable: '
                         f'{line.split(" ")[0]}' + (f' ({error})' if error else ''))

    def _send(self, line):
        """Fire-and-forget. Returns whether it went out; never raises into task code."""
        client = self._client
        if client is None:
            self._drop(line)
            self._ensure_connection()
            return False
        try:
            with self._io_lock:
                client.send(line)
            return True
        except (ShimError, OSError) as e:
            self._disconnect(client)
            self._drop(line, e)
            return False

    def _request(self, line, tag):
        """Request-response. Returns the reply payload, or None if the link is down."""
        client = self._client
        if client is None:
            self._ensure_connection()
            return None
        try:
            with self._io_lock:
                return client.request(line, tag)
        except (ShimError, OSError) as e:
            self._disconnect(client)
            self._drop(line, e)
            return None

    def _disconnect(self, client):
        with self._state_lock:
            if self._client is client:
                self._client = None
        client.close()
        self._ensure_connection()
        self._wake.set()   # reconnect now rather than after the maintainer's next nap

    # ------------------------------------------------------------------- keys ----

    def send_key(self, key, down_time=0.01):
        super().send_key(key, down_time)
        self.send_key_down(key)
        time.sleep(down_time)
        self.send_key_up(key)

    def send_key_down(self, key, activate=True):
        if activate:
            self.try_activate()
        self._send(f'KEYDOWN {self.get_key_by_str(key)}')

    def send_key_up(self, key):
        self._send(f'KEYUP {self.get_key_by_str(key)}')

    def make_lparam(self, vk_code, is_up=False):
        """Kept for parity with upstream; the shim builds the real one.

        The scan code has to come from ``MapVirtualKeyW`` *inside* Wine, so that it matches
        what the game's Unreal input layer expects [PORT.md V6] -- which is why this value
        is not what gets posted. It stays because callers and tests reach for it.
        """
        lparam = 1
        if is_up:
            lparam |= (1 << 30) | (1 << 31)
        return lparam

    def get_key_by_str(self, key):
        key = str(key)
        if key_code := vk_key_dict.get(key.upper()):
            return key_code
        return self._vk_key_scan(key)

    def _vk_key_scan(self, key):
        """``VkKeyScan`` for a character the table does not name, cached per character.

        Two deliberate differences from upstream. It caches, because a round-trip per
        keypress is unacceptable in a combat loop; and it keeps only the **low byte** of
        the result. ``VkKeyScan`` packs the shift state into the high byte, and upstream
        posts the whole value as the virtual-key code -- so an uppercase ``'A'`` becomes vk
        ``0x141``, which is not a key. The low byte is the virtual-key code.
        """
        if len(key) != 1:
            logger.error(f'cannot map {key!r} to a virtual-key code')
            return 0
        cached = self._vk_cache.get(key)
        if cached is not None:
            return cached
        if key.isascii() and key.isalnum():
            # Layout-independent for A-Z and 0-9: the VK code *is* the uppercase codepoint.
            vk = ord(key.upper())
        else:
            reply = self._request(f'VKKEYSCAN {ord(key)}', 'VKKEYSCAN')
            if reply is None:
                return 0
            try:
                scan = int(reply.strip())
            except ValueError:
                logger.error(f'VKKEYSCAN returned {reply!r}')
                return 0
            if scan < 0:
                logger.error(f'no virtual-key code for {key!r} in the game\'s layout')
                return 0
            vk = scan & 0xFF
        self._vk_cache[key] = vk
        return vk

    def input_text(self, text, activate=True):
        if activate:
            self.try_activate()
        for character in text:
            self._send(f'CHAR {ord(character)}')
            time.sleep(0.01)

    # ------------------------------------------------------------------ mouse ----

    def update_mouse_pos(self, x, y, activate=True):
        """The cached client position, and upstream's packed ``MAKELONG`` return value.

        Upstream's ``ClientToScreen`` -> hit-test -> ``ScreenToClient`` round trip cancels
        out on Linux (``hwnds`` is empty, ``top_hwnd == hwnd``), so proxying it to the shim
        would add a blocking round-trip to every mouse move for a guaranteed no-op.
        """
        self.try_activate()
        if x == -1 or y == -1:
            x, y = self.bg_mouse_pos
        else:
            x, y = self.hwnd_window.get_top_window_cords(x, y)
            self.bg_mouse_pos = (int(x), int(y))
        return (int(y) << 16) | (int(x) & 0xFFFF)

    def move(self, x, y, down_btn=0):
        long_pos = self.update_mouse_pos(x, y, True)
        mx, my = self.bg_mouse_pos
        self._send(f'MOUSEMOVE {mx} {my} {down_btn}')
        return long_pos

    def click(self, x=-1, y=-1, move_back=False, name=None, down_time=0.01, move=True,
              key="left"):
        super().click(x, y, name=name)
        if move:
            long_position = self.move(x, y)
            time.sleep(down_time)
        else:
            long_position = self.update_mouse_pos(x, y, activate=True)
        mx, my = self.bg_mouse_pos
        prefix = _button_prefix(key)
        self._send(f'{prefix}DOWN {mx} {my}')
        time.sleep(down_time)
        self._send(f'{prefix}UP {mx} {my}')
        return long_position

    def right_click(self, x=-1, y=-1, move_back=False, name=None):
        # Upstream opens with `super().right_click(...)`, and `BaseInteraction` has no
        # such method -- so calling it on Windows raises AttributeError. Nothing does:
        # `Task.right_click` routes through `click(key='right')` (`ok/task/task.py:185`).
        # Kept here for signature parity, minus the call that cannot work.
        self.update_mouse_pos(x, y)
        mx, my = self.bg_mouse_pos
        self._send(f'RDOWN {mx} {my}')
        self._send(f'RUP {mx} {my}')

    def mouse_down(self, x=-1, y=-1, name=None, key="left"):
        self.update_mouse_pos(x, y)
        mx, my = self.bg_mouse_pos
        self._send(f'{_button_prefix(key)}DOWN {mx} {my}')

    def mouse_up(self, key="left"):
        # Upstream releases at `self.mouse_pos`, which is `(0, 0)` for the life of the
        # object (`post_message.py:20`, never written again) -- so every drag and swipe
        # ends at client (0, 0). `bg_mouse_pos` is the position `update_mouse_pos` actually
        # maintains.
        mx, my = self.bg_mouse_pos
        self._send(f'{_button_prefix(key)}UP {mx} {my}')

    def swipe(self, x1, y1, x2, y2, duration=3, settle_time=0):
        self.move(x1, y1)
        time.sleep(0.1)
        self.mouse_down(x1, y1)

        dx = x2 - x1
        dy = y2 - y1
        # Upstream: `steps = int(duration / 100)`, which is 0 for every caller that takes
        # the default `duration=3` and raises ZeroDivisionError on the next line.
        steps = max(1, int(duration / 100))
        step_dx = dx / steps
        step_dy = dy / steps

        for index in range(steps):
            self.move(x1 + int(index * step_dx), y1 + int(index * step_dy),
                      down_btn=MK_LBUTTON)
            time.sleep(0.01)
        self.mouse_up()

    def scroll(self, x, y, scroll_amount):
        self.try_activate()
        logger.debug(f'scroll {x}, {y}, {scroll_amount}')
        if x > 0 and y > 0:
            self.update_mouse_pos(x, y)
            mx, my = self.bg_mouse_pos
        else:
            mx, my = 0, 0
        self._send(f'WHEEL {mx} {my} {scroll_amount}')

    # ---------------------------------------------------------------- cursor ----

    def get_cursor_pos(self):
        """The real OS cursor, in screen coordinates, cached briefly.

        Wine's screen space is the X root window's, so this and
        ``capture.get_abs_cords(...)`` speak the same coordinates.
        """
        now = time.monotonic()
        if now - self._cursor_time < CURSOR_CACHE_SECONDS:
            return self._cursor_pos
        reply = self._request('GETCURSOR', 'GETCURSOR')
        if reply is None:
            return self._cursor_pos
        try:
            x, y = reply.split()[:2]
            self._cursor_pos = (int(x), int(y))
        except ValueError:
            logger.error(f'GETCURSOR returned {reply!r}')
            return self._cursor_pos
        self._cursor_time = now
        return self._cursor_pos

    def set_cursor_pos(self, pos):
        x, y = pos
        self._cursor_pos = (int(x), int(y))
        self._cursor_time = time.monotonic()
        self._send(f'SETCURSOR {int(x)} {int(y)}')

    # ------------------------------------------------------------ activation ----

    def activate(self, hwnd=None):
        # `hwnd` is accepted for signature parity and ignored: the shim posts to the window
        # it resolved inside the prefix, and a Linux X11 window id would mean nothing there.
        self._send('ACTIVATE')

    def deactivate(self, hwnd=None):
        self._send('DEACTIVATE')

    def try_activate(self):
        # Upstream posts to `hwnd_window.hwnd` and, if different, to the current `hwnd`.
        # On Linux there is one X toplevel per Wine game, so those are the same window.
        self.activate()

    # ------------------------------------------------------------ diagnostics ----

    @property
    def hwnd(self):
        return self.hwnd_window.top_hwnd if self.hwnd_window.top_hwnd else self.hwnd_window.hwnd

    def should_capture(self):
        return True

    def find_window(self):
        """Force the shim to re-resolve the game window. Returns its HWND, or 0."""
        reply = self._request('FINDWIN', 'FINDWIN')
        if reply is None:
            return 0
        return _int_field(reply, 'hwnd')

    def get_client_geometry(self):
        """The game's client rectangle as Wine sees it: ``(x, y, width, height)``.

        Not used by the input path -- the Linux window layer owns geometry -- but it is
        what proves the shim is attached to the right window, so the exit gate asks for it.
        """
        reply = self._request('GEOM', 'GEOM')
        if reply is None:
            return None
        try:
            x, y, width, height = (int(value) for value in reply.split()[:4])
        except ValueError:
            logger.error(f'GEOM returned {reply!r}')
            return None
        return x, y, width, height

    def ping(self):
        return self._request('PING', 'PING')

    def on_destroy(self):
        with self._state_lock:
            self._closing = True
            client, self._client = self._client, None
        self._wake.set()   # let the maintainer thread notice and exit
        if client is None:
            return
        try:
            with self._io_lock:
                client.send('QUIT')
        except (ShimError, OSError):
            pass
        client.close()


def _button_prefix(key):
    if key == 'left':
        return 'L'
    if key == 'middle':
        return 'M'
    return 'R'


def _int_field(text, name):
    for part in (text or '').split():
        key, sep, value = part.partition('=')
        if sep and key == name:
            try:
                return int(value)
            except ValueError:
                return 0
    return 0
