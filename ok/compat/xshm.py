"""The X11 *pixel* path: libX11/libXext through ``ctypes``, MIT-SHM when the server offers it.

``ok/compat/x11.py`` is the window layer and uses python-xlib. This module is deliberately
its opposite number, for one reason: **python-xlib has no MIT-SHM binding at any version**
[V13] -- ``from Xlib.ext import shm`` does not exist -- and a shared-memory grab is 11x
cheaper than a wire grab (measured at 1920x1080 on this machine: ``XShmGetImage`` 1.24 ms
vs ``XGetImage`` 13.57 ms [V14]). So the pixels come through raw ``ctypes`` on their own
display connection, and the two layers share nothing but window ids.

Four details are load-bearing and easy to get wrong:

* **Xlib's default error handler calls ``exit(1)``.** Every protocol error -- and a window
  that dies between the poll thread reading its geometry and this thread grabbing it is a
  routine ``BadWindow`` or ``BadMatch`` -- would take the whole app down. ``XSetErrorHandler``
  is installed once at first use and is *process*-global (it is not per-display), which is
  acceptable here only because nothing else in the process talks to libX11: PySide6 uses
  xcb and python-xlib speaks the protocol itself. Errors are recorded and logged, never
  fatal.
* **``XDestroyImage`` is a C macro**, so it has to be called through the image's own
  ``f.destroy_image`` pointer. That is also what makes the SHM path safe: libX11 installs a
  *different* destructor on a shared image, one that does not ``free()`` the segment.
* **``bytes_per_line`` is the stride, not ``width * 4``.** Slice by it.
* **The returned frame must be a copy, and ``cv2.cvtColor`` is the cheap way to make one.**
  The SHM segment is overwritten by the next grab while ``TaskExecutor`` still holds the
  previous frame. Measured at 1080p: ``arr[:, :, :3].copy()`` 10.10 ms,
  ``np.ascontiguousarray`` 9.69 ms, ``cv2.cvtColor(..., COLOR_BGRA2BGR)`` **0.15 ms** [V14].

Nothing here raises for a routine X11 failure; ``grab()`` returns ``None`` and the caller
decides what that means. Programming errors (an unsupported pixel format) do raise, because
they are not recoverable by retrying.
"""

import ctypes
import ctypes.util
import os
import threading

import cv2
import numpy as np

from ok.util.logger import Logger

logger = Logger.get_logger(__name__)

# X protocol constants (X.h)
Z_PIXMAP = 2
ALL_PLANES = ctypes.c_ulong(-1).value
IS_VIEWABLE = 2
LSB_FIRST = 0

# XComposite (composite.h). Automatic only -- Manual makes *us* responsible for painting
# the window to the screen, which blanks the game.
COMPOSITE_REDIRECT_AUTOMATIC = 0

# System V shared memory (sys/ipc.h, sys/shm.h)
IPC_PRIVATE = 0
IPC_CREAT = 0o1000
IPC_RMID = 0


class _XImageFuncs(ctypes.Structure):
    """``XImage``'s trailing function table. ``destroy_image`` is the only one we call."""
    _fields_ = [('create_image', ctypes.c_void_p),
                ('destroy_image', ctypes.c_void_p),
                ('get_pixel', ctypes.c_void_p),
                ('put_pixel', ctypes.c_void_p),
                ('sub_image', ctypes.c_void_p),
                ('add_pixel', ctypes.c_void_p)]


class XImage(ctypes.Structure):
    _fields_ = [('width', ctypes.c_int),
                ('height', ctypes.c_int),
                ('xoffset', ctypes.c_int),
                ('format', ctypes.c_int),
                ('data', ctypes.c_void_p),
                ('byte_order', ctypes.c_int),
                ('bitmap_unit', ctypes.c_int),
                ('bitmap_bit_order', ctypes.c_int),
                ('bitmap_pad', ctypes.c_int),
                ('depth', ctypes.c_int),
                ('bytes_per_line', ctypes.c_int),
                ('bits_per_pixel', ctypes.c_int),
                ('red_mask', ctypes.c_ulong),
                ('green_mask', ctypes.c_ulong),
                ('blue_mask', ctypes.c_ulong),
                ('obdata', ctypes.c_void_p),
                ('f', _XImageFuncs)]


class Visual(ctypes.Structure):
    """Only the masks are read, but the layout has to be right to reach them."""
    _fields_ = [('ext_data', ctypes.c_void_p),
                ('visualid', ctypes.c_ulong),
                ('class', ctypes.c_int),
                ('red_mask', ctypes.c_ulong),
                ('green_mask', ctypes.c_ulong),
                ('blue_mask', ctypes.c_ulong),
                ('bits_per_rgb', ctypes.c_int),
                ('map_entries', ctypes.c_int)]


class XShmSegmentInfo(ctypes.Structure):
    _fields_ = [('shmseg', ctypes.c_ulong),
                ('shmid', ctypes.c_int),
                ('shmaddr', ctypes.c_void_p),
                ('readOnly', ctypes.c_int)]


class XWindowAttributes(ctypes.Structure):
    _fields_ = [('x', ctypes.c_int),
                ('y', ctypes.c_int),
                ('width', ctypes.c_int),
                ('height', ctypes.c_int),
                ('border_width', ctypes.c_int),
                ('depth', ctypes.c_int),
                ('visual', ctypes.c_void_p),
                ('root', ctypes.c_ulong),
                ('class', ctypes.c_int),
                ('bit_gravity', ctypes.c_int),
                ('win_gravity', ctypes.c_int),
                ('backing_store', ctypes.c_int),
                ('backing_planes', ctypes.c_ulong),
                ('backing_pixel', ctypes.c_ulong),
                ('save_under', ctypes.c_int),
                ('colormap', ctypes.c_ulong),
                ('map_installed', ctypes.c_int),
                ('map_state', ctypes.c_int),
                ('all_event_masks', ctypes.c_long),
                ('your_event_mask', ctypes.c_long),
                ('do_not_propagate_mask', ctypes.c_long),
                ('override_redirect', ctypes.c_int),
                ('screen', ctypes.c_void_p)]


class XErrorEvent(ctypes.Structure):
    # Field order is Xlib.h's, and `resourceid` comes *before* `serial` -- reading it in the
    # order the fields are usually quoted makes every logged error code garbage (a
    # destroyed window reported "BadMatch on request 0" instead of BadWindow/BadDrawable).
    _fields_ = [('type', ctypes.c_int),
                ('display', ctypes.c_void_p),
                ('resourceid', ctypes.c_ulong),
                ('serial', ctypes.c_ulong),
                ('error_code', ctypes.c_ubyte),
                ('request_code', ctypes.c_ubyte),
                ('minor_code', ctypes.c_ubyte)]


_ERROR_HANDLER_TYPE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(XErrorEvent))
_DESTROY_IMAGE_TYPE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(XImage))

_LOAD_LOCK = threading.Lock()
_libs = None
_load_error_logged = False
_error_handler = None       # kept alive for the life of the process; ctypes will not
# The last protocol error, as (error_code, request_code, minor_code, resourceid). Global
# because XSetErrorHandler is: a grabber clears it, issues one replyless request, syncs, and
# reads it back, all under its own lock. Two grabbers running concurrently could in
# principle read each other's error; there is one per capture method and ok-ww builds one.
_last_error = None


def _on_x_error(display, event):
    """Record a protocol error instead of letting Xlib's default handler exit the process."""
    global _last_error
    error = event.contents
    _last_error = (error.error_code, error.request_code, error.minor_code, error.resourceid)
    logger.debug(f'x11 pixel path: error {error.error_code} on request '
                 f'{error.request_code}.{error.minor_code} for {error.resourceid:#x}')
    return 0


class _Libs:
    """The three shared libraries, with every prototype this module uses declared."""

    def __init__(self, x11, xext, xcomposite, libc):
        self.x11 = x11
        self.xext = xext
        self.xcomposite = xcomposite
        self.libc = libc
        self._declare()

    def _declare(self):
        x11, xext, libc = self.x11, self.xext, self.libc

        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.restype = ctypes.c_int
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XSync.restype = ctypes.c_int
        x11.XSetErrorHandler.argtypes = [_ERROR_HANDLER_TYPE]
        x11.XSetErrorHandler.restype = ctypes.c_void_p
        x11.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                             ctypes.POINTER(XWindowAttributes)]
        x11.XGetWindowAttributes.restype = ctypes.c_int
        x11.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
        x11.XGetImage.restype = ctypes.POINTER(XImage)
        x11.XFreePixmap.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XFreePixmap.restype = ctypes.c_int

        xext.XShmQueryExtension.argtypes = [ctypes.c_void_p]
        xext.XShmQueryExtension.restype = ctypes.c_int
        xext.XShmCreateImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                         ctypes.c_int, ctypes.c_void_p,
                                         ctypes.POINTER(XShmSegmentInfo),
                                         ctypes.c_uint, ctypes.c_uint]
        xext.XShmCreateImage.restype = ctypes.POINTER(XImage)
        xext.XShmAttach.argtypes = [ctypes.c_void_p, ctypes.POINTER(XShmSegmentInfo)]
        xext.XShmAttach.restype = ctypes.c_int
        xext.XShmDetach.argtypes = [ctypes.c_void_p, ctypes.POINTER(XShmSegmentInfo)]
        xext.XShmDetach.restype = ctypes.c_int
        xext.XShmGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XImage),
                                      ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        xext.XShmGetImage.restype = ctypes.c_int

        libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
        libc.shmget.restype = ctypes.c_int
        libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        libc.shmat.restype = ctypes.c_void_p
        libc.shmdt.argtypes = [ctypes.c_void_p]
        libc.shmdt.restype = ctypes.c_int
        libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        libc.shmctl.restype = ctypes.c_int

        if self.xcomposite is not None:
            self.xcomposite.XCompositeQueryExtension.argtypes = [ctypes.c_void_p,
                                                                 ctypes.POINTER(ctypes.c_int),
                                                                 ctypes.POINTER(ctypes.c_int)]
            self.xcomposite.XCompositeQueryExtension.restype = ctypes.c_int
            self.xcomposite.XCompositeRedirectWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                                 ctypes.c_int]
            self.xcomposite.XCompositeRedirectWindow.restype = ctypes.c_int
            self.xcomposite.XCompositeUnredirectWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                                   ctypes.c_int]
            self.xcomposite.XCompositeUnredirectWindow.restype = ctypes.c_int
            self.xcomposite.XCompositeNameWindowPixmap.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            self.xcomposite.XCompositeNameWindowPixmap.restype = ctypes.c_ulong


def _load():
    """Load libX11/libXext (and libXcomposite if present), once. ``None`` when unavailable."""
    global _libs, _load_error_logged, _error_handler
    with _LOAD_LOCK:
        if _libs is not None:
            return _libs
        try:
            x11 = ctypes.CDLL(ctypes.util.find_library('X11') or 'libX11.so.6')
            xext = ctypes.CDLL(ctypes.util.find_library('Xext') or 'libXext.so.6')
        except OSError as e:
            if not _load_error_logged:
                _load_error_logged = True
                logger.error(f'libX11/libXext are not loadable, X11 capture is unavailable: {e}')
            return None
        try:
            xcomposite = ctypes.CDLL(ctypes.util.find_library('Xcomposite') or 'libXcomposite.so.1')
        except OSError as e:
            xcomposite = None
            logger.info(f'libXcomposite is absent; the X11_Composite capture method is unavailable: {e}')
        libc = ctypes.CDLL(None, use_errno=True)
        _libs = _Libs(x11, xext, xcomposite, libc)
        # Install *before* the first request. Xlib's default handler exits the process on a
        # BadWindow, and a window that vanishes mid-grab is routine here.
        _error_handler = _ERROR_HANDLER_TYPE(_on_x_error)
        x11.XSetErrorHandler(_error_handler)
        return _libs


def available():
    """True when the pixel path can run: the libraries load and ``DISPLAY`` is set."""
    return _load() is not None and bool(os.environ.get('DISPLAY'))


def composite_available():
    """True when libXcomposite loaded. The *server* is only asked once a capture starts."""
    return _load() is not None and _load().xcomposite is not None


def visual_masks(visual):
    """``(red, green, blue)`` masks of a ``Visual*``, or ``None``.

    Needed because a grab from a **Pixmap** comes back with the image's masks zeroed:
    ``XGetImage`` and ``XShmGetImage`` both fill them in from the *reply's* visual id, and a
    pixmap has no visual. That is the composite path's whole picture, so the window's own
    visual is read here and passed down as the fallback.
    """
    if not visual:
        return None
    fields = ctypes.cast(ctypes.c_void_p(visual), ctypes.POINTER(Visual)).contents
    if not (fields.red_mask or fields.green_mask or fields.blue_mask):
        return None
    return int(fields.red_mask), int(fields.green_mask), int(fields.blue_mask)


def _channel_indices(image, masks=None):
    """Byte offsets of B, G, R inside each 32-bit pixel, from the image's own masks.

    Verified BGRA on this machine (``byte_order`` LSBFirst, ``R=ff0000 G=ff00 B=ff``,
    ``bits_per_pixel=32``) [V14], which is the ``(0, 1, 2)`` fast path below. The general
    form is here rather than an assertion because a big-endian server or an unusual visual
    is a wrong *picture*, not a crash, and a silently colour-swapped frame is the hardest
    kind of bug to see in a template matcher.
    """

    def index(mask):
        if not mask:
            return None
        shift = (int(mask) & -int(mask)).bit_length() - 1
        byte = shift // 8
        return byte if image.byte_order == LSB_FIRST else 3 - byte

    red, green, blue = image.red_mask, image.green_mask, image.blue_mask
    if not (red or green or blue) and masks:
        red, green, blue = masks
    return index(blue), index(green), index(red)


def image_to_bgr(image, masks=None):
    """``XImage*`` -> a fresh contiguous ``(h, w, 3)`` BGR array. Never a view.

    ``masks`` is the ``(red, green, blue)`` fallback for a grab from a pixmap, whose image
    comes back with its own masks zeroed; see :func:`visual_masks`.
    """
    frame = image.contents
    if frame.bits_per_pixel != 32:
        raise ValueError(f'unsupported X11 pixel format: {frame.bits_per_pixel} bits per pixel '
                         f'at depth {frame.depth}; only 32-bit TrueColor is supported')
    if not frame.data:
        raise ValueError('the X11 image carries no pixel data')
    stride = frame.bytes_per_line
    if stride % 4:
        raise ValueError(f'unsupported X11 stride {stride} for a 32-bit image')
    height, width, pixels_per_row = frame.height, frame.width, stride // 4
    buffer = (ctypes.c_ubyte * (stride * height)).from_address(frame.data)
    array = np.frombuffer(buffer, dtype=np.uint8).reshape(height, pixels_per_row, 4)

    blue, green, red = _channel_indices(frame, masks)
    if (blue, green, red) == (0, 1, 2):
        # The measured path: one 0.15 ms pass that drops the alpha byte and copies.
        bgr = cv2.cvtColor(array, cv2.COLOR_BGRA2BGR)
    else:
        if None in (blue, green, red):
            raise ValueError(f'X11 image has no RGB masks: {frame.red_mask:#x} '
                             f'{frame.green_mask:#x} {frame.blue_mask:#x}')
        bgr = np.ascontiguousarray(array[:, :, [blue, green, red]])
    if pixels_per_row != width:
        bgr = np.ascontiguousarray(bgr[:, :width])
    return bgr


class X11Grabber:
    """One display connection, one reusable SHM segment, for one window at a time.

    Not shared between capture methods: ``close()`` tears the connection down, and the
    composite path leaves per-window state (a redirect and a named pixmap) that has to be
    released with it.
    """

    def __init__(self, use_composite=False):
        self.use_composite = use_composite
        self._lock = threading.RLock()
        self._display = None
        self._shm_checked = False
        self._shm_usable = False
        self._image = None
        self._shm_info = None
        self._image_size = (0, 0)
        self._image_depth = 0
        self._redirected = 0
        self._pixmap = 0
        self._composite_checked = False
        self._composite_failed = False

    # --- connection ---------------------------------------------------------------------

    def _open(self):
        """Open (or reuse) this grabber's own display connection. Caller holds the lock."""
        if self._display is not None:
            return self._display
        libs = _load()
        if libs is None:
            return None
        name = os.environ.get('DISPLAY')
        if not name:
            logger.error('DISPLAY is not set; X11 capture needs X11 or Xwayland')
            return None
        display = libs.x11.XOpenDisplay(name.encode())
        if not display:
            logger.error(f'cannot connect to X display {name!r} for capture')
            return None
        self._display = display
        return display

    def close(self):
        with self._lock:
            libs = _libs
            if libs is not None and self._display is not None:
                self._release_pixmap(libs)
                self._unredirect(libs)
                self._free_image(libs)
                try:
                    libs.x11.XCloseDisplay(self._display)
                except Exception as e:
                    logger.debug(f'XCloseDisplay failed: {e}')
            self._display = None
            self._shm_checked = False
            self._shm_usable = False

    # --- shared memory ------------------------------------------------------------------

    def _free_image(self, libs):
        """Detach and destroy the reusable image. Order matters: detach, destroy, shmdt."""
        if self._image is None:
            return
        image, info = self._image, self._shm_info
        self._image, self._shm_info, self._image_size, self._image_depth = None, None, (0, 0), 0
        try:
            if info is not None and self._display is not None:
                libs.xext.XShmDetach(self._display, ctypes.byref(info))
                libs.x11.XSync(self._display, 0)
            # XDestroyImage is a macro; go through the image's own destructor. libX11 gives
            # a shared image a destructor that does *not* free the segment, which is why
            # the shmdt below is ours to do and is correct only in this order.
            _DESTROY_IMAGE_TYPE(image.contents.f.destroy_image)(image)
            if info is not None and info.shmaddr:
                libs.libc.shmdt(info.shmaddr)
        except Exception as e:
            logger.debug(f'releasing the shared image failed: {e}')

    def _shm_supported(self, libs, display):
        if not self._shm_checked:
            self._shm_checked = True
            try:
                self._shm_usable = bool(libs.xext.XShmQueryExtension(display))
            except Exception as e:
                logger.info(f'XShmQueryExtension failed, falling back to XGetImage: {e}')
                self._shm_usable = False
            if not self._shm_usable:
                # A remote or namespaced server has no shared memory with us. 13.57 ms per
                # frame instead of 1.24 ms [V14] -- slower, still far inside what ok-ww needs.
                logger.info('MIT-SHM is unavailable on this display; using XGetImage')
        return self._shm_usable

    def _ensure_shm_image(self, libs, display, visual, depth, width, height):
        """The reusable shared image, reallocated only when the size or depth changes."""
        if (self._image is not None and self._image_size == (width, height)
                and self._image_depth == depth):
            return self._image
        self._free_image(libs)

        info = XShmSegmentInfo()
        image = libs.xext.XShmCreateImage(display, visual, depth, Z_PIXMAP, None,
                                          ctypes.byref(info), width, height)
        if not image:
            logger.warning('XShmCreateImage failed; falling back to XGetImage')
            self._shm_usable = False
            return None

        size = image.contents.bytes_per_line * image.contents.height
        shmid = libs.libc.shmget(IPC_PRIVATE, size, IPC_CREAT | 0o600)
        if shmid < 0:
            logger.warning(f'shmget({size}) failed: {os.strerror(ctypes.get_errno())}; '
                           f'falling back to XGetImage')
            _DESTROY_IMAGE_TYPE(image.contents.f.destroy_image)(image)
            self._shm_usable = False
            return None
        address = libs.libc.shmat(shmid, None, 0)
        if not address or address == ctypes.c_void_p(-1).value:
            logger.warning('shmat failed; falling back to XGetImage')
            libs.libc.shmctl(shmid, IPC_RMID, None)
            _DESTROY_IMAGE_TYPE(image.contents.f.destroy_image)(image)
            self._shm_usable = False
            return None

        info.shmid = shmid
        info.shmaddr = address
        info.readOnly = 0
        image.contents.data = address

        global _last_error
        _last_error = None
        attached = libs.xext.XShmAttach(display, ctypes.byref(info))
        libs.x11.XSync(display, 0)
        # XShmAttach carries no reply, so a refusal (a server that does not share our
        # memory namespace) arrives at the error handler rather than in the return value.
        if not attached or _last_error is not None:
            logger.warning('XShmAttach was refused; falling back to XGetImage')
            libs.libc.shmctl(shmid, IPC_RMID, None)
            _DESTROY_IMAGE_TYPE(image.contents.f.destroy_image)(image)
            libs.libc.shmdt(address)
            self._shm_usable = False
            return None
        # Mark the segment destroyed now: the kernel reclaims it when the last attachment
        # goes away, so it cannot leak even if this process is killed.
        libs.libc.shmctl(shmid, IPC_RMID, None)

        self._image, self._shm_info = image, info
        self._image_size, self._image_depth = (width, height), depth
        return image

    # --- composite ----------------------------------------------------------------------

    def _release_pixmap(self, libs):
        if self._pixmap and self._display is not None:
            try:
                libs.x11.XFreePixmap(self._display, self._pixmap)
            except Exception as e:
                logger.debug(f'XFreePixmap failed: {e}')
        self._pixmap = 0

    def _unredirect(self, libs):
        if self._redirected and libs.xcomposite is not None and self._display is not None:
            try:
                libs.xcomposite.XCompositeUnredirectWindow(self._display, self._redirected,
                                                           COMPOSITE_REDIRECT_AUTOMATIC)
                libs.x11.XSync(self._display, 0)
            except Exception as e:
                logger.debug(f'XCompositeUnredirectWindow failed: {e}')
        self._redirected = 0

    def _composite_pixmap(self, libs, display, wid):
        """The window's offscreen pixmap, redirecting it first. ``0`` when unavailable.

        ``Automatic`` and never ``Manual``: under ``Manual`` the server stops painting the
        window to the screen and expects us to do it, which blanks the game.

        **The pixmap is re-named on every grab, and that is not an oversight.** A name is a
        handle onto the backing pixmap *as it is now*; a client that presents by flipping --
        which is what DXVK does for the game, through the Present extension -- gets a new
        backing pixmap per frame and the old name keeps the frame it was taken on. Measured
        against the running game at 2560x1440: with the name cached, six grabs 0.25 s apart
        differed by exactly 0.0, i.e. a frozen picture that looks like a working capture;
        re-named each grab, the same six differed by 28-57. The cost is one XID and two
        replyless requests, 3.56 -> 5.41 ms per frame, which is the direct path's 5.18 ms.
        """
        global _last_error
        if libs.xcomposite is None or self._composite_failed:
            return 0
        if not self._composite_checked:
            self._composite_checked = True
            event_base, error_base = ctypes.c_int(), ctypes.c_int()
            if not libs.xcomposite.XCompositeQueryExtension(display, ctypes.byref(event_base),
                                                            ctypes.byref(error_base)):
                # The library is installed but the server does not offer the extension
                # (Xvfb without `+extension COMPOSITE`, for one). Redirecting anyway would
                # be a BadRequest that the error handler swallows; say so once instead.
                logger.info('the X server has no Composite extension; capturing the window directly')
                self._composite_failed = True
                return 0
        if self._redirected != wid:
            self._release_pixmap(libs)
            self._unredirect(libs)
            _last_error = None
            libs.xcomposite.XCompositeRedirectWindow(display, wid, COMPOSITE_REDIRECT_AUTOMATIC)
            libs.x11.XSync(display, 0)
            if _last_error is not None:
                # BadAccess: another client (a compositing WM) already redirects this
                # window -- which means its contents are already backed offscreen and the
                # plain window grab works. Anything else is a real failure. Either way,
                # stop asking.
                logger.info(f'XCompositeRedirectWindow refused (error {_last_error[0]}); '
                            f'capturing the window directly')
                self._composite_failed = True
                return 0
            self._redirected = wid

        self._release_pixmap(libs)
        _last_error = None
        pixmap = libs.xcomposite.XCompositeNameWindowPixmap(display, wid)
        libs.x11.XSync(display, 0)
        if not pixmap or _last_error is not None:
            logger.info('XCompositeNameWindowPixmap failed; capturing the window directly')
            self._composite_failed = True
            return 0
        self._pixmap = pixmap
        return pixmap

    # --- the grab -----------------------------------------------------------------------

    def window_geometry(self, wid):
        """``(width, height, border_width, depth, map_state)``, or ``None`` if it is gone."""
        with self._lock:
            libs = _load()
            display = self._open()
            if libs is None or display is None or not wid:
                return None
            attributes = XWindowAttributes()
            global _last_error
            _last_error = None
            if not libs.x11.XGetWindowAttributes(display, wid, ctypes.byref(attributes)):
                return None
            return (attributes.width, attributes.height, attributes.border_width,
                    attributes.depth, attributes.map_state)

    def grab(self, wid, x, y, width, height):
        """Capture ``width x height`` at ``(x, y)`` inside the window. BGR array, or ``None``.

        The rectangle is clamped to the window's *current* size: the caller's numbers come
        from a 0.2 s poll and the window can have been resized since, and ``XGetImage``
        answers a rectangle that leaves the drawable with ``BadMatch`` rather than with a
        clipped image.
        """
        global _last_error
        with self._lock:
            libs = _load()
            display = self._open()
            if libs is None or display is None or not wid:
                return None

            attributes = XWindowAttributes()
            _last_error = None
            if not libs.x11.XGetWindowAttributes(display, wid, ctypes.byref(attributes)):
                logger.debug(f'XGetWindowAttributes failed for {wid:#x}; the window is gone')
                return None
            if attributes.map_state != IS_VIEWABLE:
                # Not capturable, and not an error: an unmapped or iconified window has no
                # pixels under rootless Xwayland [V7]. The capture method turns this into
                # the message the user can act on.
                return None

            x = max(0, min(int(x), attributes.width - 1))
            y = max(0, min(int(y), attributes.height - 1))
            width = min(int(width), attributes.width - x)
            height = min(int(height), attributes.height - y)
            if width <= 0 or height <= 0:
                return None

            drawable, offset_x, offset_y = wid, 0, 0
            if self.use_composite:
                pixmap = self._composite_pixmap(libs, display, wid)
                if pixmap:
                    # The named pixmap includes the window's border on every side.
                    drawable = pixmap
                    offset_x = offset_y = attributes.border_width

            image = None
            shared = False
            if self._shm_supported(libs, display):
                image = self._ensure_shm_image(libs, display, attributes.visual,
                                               attributes.depth, width, height)
                if image is not None:
                    _last_error = None
                    if libs.xext.XShmGetImage(display, drawable, image,
                                              x + offset_x, y + offset_y, ALL_PLANES) and _last_error is None:
                        shared = True
                    else:
                        logger.debug(f'XShmGetImage failed for {drawable:#x} '
                                     f'({width}x{height}+{x}+{y})')
                        image = None

            if image is None:
                _last_error = None
                image = libs.x11.XGetImage(display, drawable, x + offset_x, y + offset_y,
                                           width, height, ALL_PLANES, Z_PIXMAP)
                if not image or _last_error is not None:
                    logger.debug(f'XGetImage failed for {drawable:#x} ({width}x{height}+{x}+{y})')
                    if image:
                        # An error *and* an image: not a shape libX11 produces today, but a
                        # wire-path image is this function's to free and leaking one per
                        # frame is not a failure mode worth leaving open.
                        _DESTROY_IMAGE_TYPE(image.contents.f.destroy_image)(image)
                    return None

            try:
                return image_to_bgr(image, visual_masks(attributes.visual))
            finally:
                if not shared:
                    _DESTROY_IMAGE_TYPE(image.contents.f.destroy_image)(image)

    @property
    def shm_active(self):
        """True once a shared segment is attached -- for diagnostics and the tests."""
        with self._lock:
            return self._image is not None
