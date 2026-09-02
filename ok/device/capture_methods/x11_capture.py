"""``X11CaptureMethod`` -- the Linux per-window capture backend.

It subclasses ``BaseWindowsCaptureMethod`` rather than ``BaseCaptureMethod``, because that
is the shape ``update_capture_method`` builds (``target_method(hwnd_window)``, then
``.hwnd_window`` and ``.exit_event`` assigned) and because ``CombatCheck`` and
``MouseResetTask`` call ``interaction.capture.get_abs_cords(...)``, which only the windows
base provides.

Only ``do_get_frame`` is real work: ``BaseCaptureMethod.get_frame`` already checks the exit
event, drops frames of 10 px or less, tracks ``self._size``, and wraps everything in
``CaptureException``.

Three decisions are worth knowing before changing anything here:

* **``clickable()`` is overridden to ``True``.** The base returns ``hwnd_window.visible``,
  and ``visible`` on this port means *foreground* [V15] -- it is False for the whole of
  background play, which is what the port exists for. The override costs nothing and is
  correct, but it is not what makes background play work: ``PostMessageInteraction`` (and
  so ``WinePostMessageInteraction``) never asks a capture object whether it is clickable.
* **The captured rectangle is the *client* rectangle, cropped like the overlay's.** X11 has
  no window rect -- the client window *is* the client area, decorations belong to the WM's
  frame -- so the crop is computed from ``client_width``/``client_height``, exactly as
  ``HwndWindow.get_capture_origin`` does. Using ``real_width``/``real_height`` the way
  ``BitBltCaptureMethod`` does would be wrong here: on Linux those are the *window's* size
  [V18], not a letterboxed child's, so they would undo the aspect-ratio crop.
* **A minimized window returns None, it does not raise.** A ``CaptureException`` out of a
  task reaches ``TaskExecutor.py:639`` and is answered with ``task.disable()`` -- a minimize
  would switch the task off instead of pausing it. The window layer already pauses the
  executor and tells the user, reversibly, when ``pos_valid`` goes False. Occlusion needs no
  treatment at all on Xwayland [V7].

``X11_Composite`` is the same class with ``use_composite`` set, mirroring how
``BitBlt_RenderFull`` is ``BitBltCaptureMethod`` with ``bitblt.render_full`` set. It exists
for plain (non-compositing) X11 sessions, where an occluded window's pixels are genuinely
not in the framebuffer; on Xwayland, and under any compositing WM, the direct path already
captures an occluded window.
"""

import threading

from ok.compat import x11, xshm
from ok.util.logger import Logger

from ok.device.capture_methods.base import BaseWindowsCaptureMethod
from ok.device.capture_methods.geometry import get_crop_point

logger = Logger.get_logger(__name__)

# Set by `update_capture_method` from the configured method name, as `bitblt.render_full`
# is. Module state rather than a constructor argument because `get_capture` reuses a live
# capture object across reconfigurations and only ever calls `target_method(hwnd)`.
use_composite = False


def x11_capture_available():
    """True when the pixel path can run at all: the libraries load, and a display answers.

    The sibling of ``windows_graphics_available()``, and used the same way -- to keep
    ``update_capture_method`` from selecting a backend that can never produce a frame,
    so the next entry in the user's ``capture_method`` list gets its turn.

    ``xshm.available()`` alone is not enough: it proves libX11/libXext loaded and
    ``DISPLAY`` is set, and a stale ``DISPLAY`` passes both while every grab fails.
    ``x11.available()`` is the window layer's own guard and actually opens a connection,
    on a connection it then keeps.
    """
    return xshm.available() and x11.available()


def capture_rect(hwnd_window, window_width=0, window_height=0):
    """The window-local rectangle to grab: ``(x, y, width, height)``.

    ``window_width``/``window_height`` are the window's live size, used only when the poll
    thread has not filled in the geometry yet (the first frame can be asked for before the
    first ``do_update_window_size``); pass 0 to skip that fallback.

    The ``real_*`` branch is upstream's and is dead on Linux -- ``find_hwnd`` reports
    ``(0, 0)`` offsets because Wine gives one X toplevel per game [V11] -- but it is kept so
    that this reads as the sibling of ``BitBltCaptureMethod.do_get_frame`` and stays correct
    if that ever changes.
    """
    width = hwnd_window.width
    height = hwnd_window.height
    if width <= 0 or height <= 0:
        return 0, 0, window_width, window_height
    if hwnd_window.real_x_offset != 0 or hwnd_window.real_y_offset != 0:
        return hwnd_window.real_x_offset, hwnd_window.real_y_offset, width, height
    client_width = hwnd_window.client_width or width
    client_height = hwnd_window.client_height or height
    x, y = get_crop_point(client_width, client_height, width, height)
    return max(x, 0), max(y, 0), width, height


class X11CaptureMethod(BaseWindowsCaptureMethod):
    name = "X11"
    short_description = "fast, works in the background"
    description = (
            "\nCaptures the game window's own pixels through X11/Xwayland, "
            + "\nusing MIT-SHM where the server offers it. "
            + "\nWorks while the window is behind others; it cannot capture a minimized window. "
    )

    def __init__(self, hwnd_window):
        super().__init__(hwnd_window)
        self.lock = threading.Lock()
        self.grabber = xshm.X11Grabber(use_composite=use_composite)
        self._minimized_reported = False

    def do_get_frame(self):
        with self.lock:
            hwnd_window = self.hwnd_window
            if hwnd_window is None or not hwnd_window.hwnd:
                return None
            hwnd = hwnd_window.hwnd
            if self.grabber.use_composite != use_composite:
                # The user switched X11 <-> X11_Composite in the GUI: `get_capture` hands
                # back this same object, so the grabber is what has to be rebuilt.
                self.grabber.close()
                self.grabber = xshm.X11Grabber(use_composite=use_composite)

            x, y, width, height = capture_rect(hwnd_window)
            if width <= 0 or height <= 0:
                # Before the first `do_update_window_size`, the only size that exists is the
                # server's. Asked for only then: `grab` reads the window's attributes anyway,
                # so doing this every frame would be a second round trip for nothing.
                geometry = self.grabber.window_geometry(hwnd)
                if geometry is None:
                    return None
                x, y, width, height = 0, 0, geometry[0], geometry[1]
            frame = self.grabber.grab(hwnd, x, y, width, height)
            if frame is None and x11.exists(hwnd) and x11.is_minimized(hwnd):
                # Deliberately NOT a CaptureException. A CaptureException out of a task
                # reaches `TaskExecutor`'s `except Exception` (TaskExecutor.py:639), which
                # calls `task.disable()` (:644) -- so raising here turns a minimize into a
                # switched-off task the user has to turn back on. The window layer already
                # handles this correctly and reversibly: `pos_valid` goes False
                # (x11_window.py:410), the executor is paused and the user is told
                # "Paused because game window is minimized or out of screen!" (:411-417),
                # and play resumes when the window comes back.
                #
                # `exists` first, and it is not belt and braces: `is_minimized`'s last resort
                # is "not viewable", which a window id that no longer names anything answers
                # True [V7].
                if not self._minimized_reported:
                    self._minimized_reported = True
                    logger.info(f'{hwnd:#x} is minimized; X11 cannot capture it. '
                                f'The window layer pauses the executor and notifies.')
                return None
            if frame is not None:
                self._minimized_reported = False
            return frame

    def get_name(self):
        # `composite_active`, not `use_composite`: the composite path degrades to the
        # direct grab silently, and a name that keeps claiming otherwise is the reason a
        # composite problem is hard to see in a log.
        return 'X11_Composite' if self.grabber.composite_active else 'X11'

    def use_composite_path(self, composite):
        """Switch the grabber between the direct and the XComposite path, now.

        Called by ``update_capture_method``: ``get_capture`` hands back this same object
        across a reconfiguration, so the path cannot be a constructor argument -- and
        leaving the rebuild to the next ``do_get_frame`` means ``get_name()`` reports the
        old path until a frame happens to be asked for.
        """
        with self.lock:
            if self.grabber.use_composite != composite:
                self.grabber.close()
                self.grabber = xshm.X11Grabber(use_composite=composite)

    def clickable(self):
        # NOT `hwnd_window.visible`: that is a foreground test [V15] and is False for the
        # whole of background play.
        return True

    def connected(self):
        return self._hwnd_window is not None and self._hwnd_window.hwnd > 0 and x11.exists(
            self._hwnd_window.hwnd)

    def close(self):
        with self.lock:
            self.grabber.close()
