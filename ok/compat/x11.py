"""Thin, defensive wrapper around python-xlib for the Linux window layer.

Everything Phase 2 needs from X11 lives here, so `ok/compat/window_x11.py` and
`ok/device/capture_methods/x11_window.py` read as window logic rather than as protocol
plumbing. Three rules hold throughout:

* **Nothing raises.** X11 is asynchronous and the game window can vanish between two
  requests in the same poll, so every entry point returns a documented empty value
  (``None``, ``0``, ``[]``, ``False``) instead of propagating ``Xlib.error.*``. The callers
  are upstream's window loop and ``find_hwnd``, neither of which has an error path.
* **One display, one lock.** ``Xlib.display.Display`` is not thread-safe and
  ``X11Window`` polls from a daemon thread while the Qt thread reads geometry, so every
  request goes through ``_LOCK``. A dead connection (X server restart, Xwayland respawn)
  is dropped and reopened on the next call.
* **The pixel path does not come through here.** python-xlib has no MIT-SHM binding at any
  version, so Phase 3 binds libX11/libXext through ``ctypes`` on its own connection. This
  module is the window layer only.

Window ids are plain ints, which is what makes them usable as ``hwnd`` -- every
``if hwnd > 0`` test in upstream keeps working unchanged.
"""

import os
import threading
import time

from ok.util.logger import Logger

logger = Logger.get_logger(__name__)

# WM_STATE states (ICCCM 4.1.3.1). Withdrawn windows are not real clients.
WITHDRAWN_STATE = 0
NORMAL_STATE = 1
ICONIC_STATE = 3

_LOCK = threading.RLock()
_display = None
_atoms = {}
_import_error_logged = False


def _xlib():
    """Return the ``Xlib`` package, or ``None`` if python-xlib is not installed."""
    global _import_error_logged
    try:
        import Xlib
        import Xlib.display  # noqa: F401  -- imported for its side effect on the package
        import Xlib.error  # noqa: F401
        import Xlib.X  # noqa: F401
        import Xlib.protocol.event  # noqa: F401
        return Xlib
    except ImportError as e:
        if not _import_error_logged:
            _import_error_logged = True
            logger.error(f'python-xlib is not installed, the X11 window layer is unavailable: {e}')
        return None


def _open():
    """Open (or reuse) the shared display connection. Caller must hold ``_LOCK``."""
    global _display
    if _display is not None:
        return _display
    xlib = _xlib()
    if xlib is None:
        return None
    if not os.environ.get('DISPLAY'):
        logger.error('DISPLAY is not set; ok-script needs X11 or Xwayland')
        return None
    try:
        _display = xlib.display.Display()
        # Errors for requests that carry no reply (ConfigureWindow, SendEvent, ...) arrive
        # asynchronously; without a handler python-xlib prints them to stderr. They are
        # normal here -- a window can die mid-poll -- so log and carry on.
        _display.set_error_handler(_on_async_error)
    except Exception as e:
        _display = None
        logger.error(f'cannot connect to X display {os.environ.get("DISPLAY")!r}: {e}')
    return _display


def _on_async_error(error, request):
    logger.debug(f'x11 async error: {error} on {request}')


def _reset():
    """Drop a broken connection. Caller must hold ``_LOCK``."""
    global _display, _atoms
    if _display is not None:
        try:
            _display.close()
        except Exception:
            pass
    _display = None
    _atoms = {}


def available():
    """True when python-xlib is importable and a display connection can be opened."""
    with _LOCK:
        return _open() is not None


def close():
    with _LOCK:
        _reset()


def _call(func, default=None, what=''):
    """Run ``func(display)`` under the lock, mapping every X11 failure onto ``default``."""
    xlib = _xlib()
    if xlib is None:
        return default
    with _LOCK:
        d = _open()
        if d is None:
            return default
        try:
            return func(d)
        except (xlib.error.BadWindow, xlib.error.BadDrawable, xlib.error.BadMatch,
                xlib.error.BadValue, xlib.error.BadAccess):
            # The window died, or was never ours. Routine.
            return default
        except (xlib.error.ConnectionClosedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.warning(f'x11 connection lost ({what or "request"}): {e}')
            _reset()
            return default
        except Exception as e:
            logger.debug(f'x11 {what or "request"} failed: {e}')
            return default


def atom(name):
    """Intern an atom, memoised. 0 if the display is unavailable."""
    cached = _atoms.get(name)
    if cached is not None:
        return cached
    value = _call(lambda d: d.get_atom(name), 0, f'get_atom({name})')
    if value:
        _atoms[name] = value
    return value


def _window(d, wid):
    return d.create_resource_object('window', wid)


def _prop(d, wid, name):
    """Raw property value list, or ``None``. ``AnyPropertyType`` -- callers know the shape."""
    import Xlib.X
    prop = _window(d, wid).get_full_property(d.get_atom(name), Xlib.X.AnyPropertyType)
    return None if prop is None else prop.value


def get_property(wid, name):
    return _call(lambda d: _prop(d, wid, name), None, f'get_property({name})')


# --- enumeration -----------------------------------------------------------------------

def _walk_for_clients(d, wid, depth, out):
    """Depth-limited hunt for windows carrying WM_STATE, for WMs without _NET_CLIENT_LIST."""
    import Xlib.X
    if depth > 3:
        return
    for child in _window(d, wid).query_tree().children:
        try:
            if child.get_full_property(d.get_atom('WM_STATE'), Xlib.X.AnyPropertyType) is not None:
                out.append(child.id)
            else:
                _walk_for_clients(d, child.id, depth + 1, out)
        except Exception:
            continue


def _frames_a_known_client(child, seen):
    """True when ``child`` is a reparenting WM's frame around a client we already have.

    Under a *reparenting* WM (kwin_x11, Mutter on X11, Xfwm, Openbox -- all plausible for
    a Proton session) the root's children are the WM's frames and the clients live one
    level down, so source 3 would add one frame per managed window on top of the clients
    source 1 already returned [P2-11]. Not a correctness bug -- a frame carries no
    ``_NET_WM_PID`` and no name, so it falls out at ``find_hwnd``'s first filter -- but it
    is measurable. Measured on a nested Xwayland driven by a minimal reparenting WM,
    10 clients, against the same server and clients under a non-reparenting one:

    ==================  ==========  ==============  ==============
    ``find_hwnd``       flat        reparent, kept  reparent, this
    ==================  ==========  ==============  ==============
    per call            3.17 ms     5.28 ms         3.93 ms
    ``list_clients``    0.11 ms     0.53 ms         1.00 ms
    ``no _NET_WM_PID``  0 lines     10 lines        0 lines
    ==================  ==========  ==============  ==============

    ``list_clients`` alone gets *slower* -- one ``QueryTree`` per frame is a round trip
    the old loop did not make -- but it buys back three per managed window in
    ``find_hwnd``, which is the call that runs on the 0.2s poll thread and the only one
    that matters; both of its callers filter by name or by pid and were paying for the
    frames either way. On a non-reparenting WM nothing changes (3.17 -> 3.17 ms): the
    clients are the root's own children and are skipped by ``child.id in seen`` above.
    The rejection-line column is P2-6's message, whose whole purpose is signal.

    The test is "does it contain something we already have", not "is it override-redirect"
    or "is it unnamed": an override-redirect toplevel has no child in ``seen`` -- nothing
    else in the tree holds it, which is precisely why source 3 exists -- so P2-7's window
    is kept. A frame the WM built around a client that reached us any other way is not.
    """
    try:
        return any(c.id in seen for c in child.query_tree().children)
    except Exception:
        return False


def list_clients():
    """Toplevel client windows, in the WM's own order. ``[]`` when X11 is unavailable.

    Three sources, **unioned** rather than tried in order, de-duplicated, best first:

    1. ``_NET_CLIENT_LIST`` -- EWMH. KWin, Mutter and every WM a Proton game realistically
       runs under publish it; verified present on this machine's Xwayland.
    2. A WM_STATE walk of the root's descendants, for a managed but non-EWMH WM.
    3. The root's own viewable children, for a bare X server with no window manager at all
       (which is also what CI gets under Xvfb).

    Source 3 is unioned in rather than being a fallback, and it keeps override-redirect
    windows. A window the WM does not manage is in none of the WM's lists:
    ``_NET_CLIENT_LIST`` holds only managed clients and ``WM_STATE`` is a property the
    *WM* sets, so an override-redirect toplevel -- how a client takes the screen without
    asking, a shape fullscreen-exclusive Wine can produce -- is invisible to both.
    Returning on the first non-empty source made 3 dead code under any EWMH WM, i.e.
    always. What source 3 must not do is hand back a *reparenting* WM's frames on top of
    the clients source 1 already gave us, one per managed window; ``_frames_a_known_client``
    drops those, and only those -- see its docstring for the measurement [P2-11].

    Source 2 stays a fallback, because it is the expensive one: recursing the tree costs
    ~6 ms against ~1.7 ms for sources 1 and 3 together on a 41-child root, and it can only
    find windows that source 1 already has whenever EWMH is present. It runs when
    ``_NET_CLIENT_LIST`` is absent or empty -- a managed but non-EWMH WM, where the root's
    children are the WM's frames and the clients are one level down.
    """

    def run(d):
        import Xlib.X
        root = d.screen().root
        out = []
        seen = set()

        def add(wid):
            wid = int(wid)
            if wid not in seen:
                seen.add(wid)
                out.append(wid)

        for wid in (_prop(d, root.id, '_NET_CLIENT_LIST') or []):
            add(wid)
        if not out:
            walked = []
            _walk_for_clients(d, root.id, 0, walked)
            for wid in walked:
                add(wid)
        for child in root.query_tree().children:
            if child.id in seen:
                continue
            try:
                attributes = child.get_attributes()
                if (attributes.map_state == Xlib.X.IsViewable
                        and attributes.win_class == Xlib.X.InputOutput):
                    if _frames_a_known_client(child, seen):
                        continue
                    add(child.id)
            except Exception:
                continue
        return out

    return _call(run, [], 'list_clients') or []


def exists(wid):
    """True when the id still names a live window. The Linux ``win32gui.IsWindow``."""
    if not wid:
        return False
    return _call(lambda d: _window(d, wid).get_attributes() is not None, False, 'exists') or False


# --- identity --------------------------------------------------------------------------

def get_pid(wid):
    """``_NET_WM_PID``, the only reliable X11 -> Linux-process key under Proton [V8][V11].

    ``WM_CLASS`` is useless here: every Proton window reports ``steam_proton``.
    """
    value = get_property(wid, '_NET_WM_PID')
    if not value:
        return 0
    try:
        return int(value[0])
    except (TypeError, ValueError, IndexError):
        return 0


def get_name(wid):
    """Window title: ``_NET_WM_NAME`` (UTF-8) first, then ``WM_NAME``. ``''`` if unnamed.

    The two properties are typed differently and decoding them the same way mangles
    accented titles: ``_NET_WM_NAME`` is UTF8_STRING, but ``WM_NAME`` is ICCCM ``STRING``,
    which is Latin-1 (ICCCM 2.7.1). Only a client that sets no ``_NET_WM_NAME`` reaches
    the fallback.
    """
    encoding = 'utf-8'
    value = get_property(wid, '_NET_WM_NAME')
    if not value:
        value = get_property(wid, 'WM_NAME')
        encoding = 'latin-1'
    if not value:
        return ''
    if isinstance(value, bytes):
        return value.decode(encoding, 'replace')
    if isinstance(value, str):
        return value
    try:
        return bytes(value).decode(encoding, 'replace')
    except Exception:
        return ''


# --- geometry --------------------------------------------------------------------------

def get_abs_geometry(wid):
    """``(x, y, width, height)`` of the window's own area in root coordinates, or ``None``.

    ``get_geometry`` alone is parent-relative, which is wrong under any reparenting WM, so
    the origin comes from ``TranslateCoordinates``.
    """

    def run(d):
        win = _window(d, wid)
        geom = win.get_geometry()
        coords = d.screen().root.translate_coords(win, 0, 0)
        return int(coords.x), int(coords.y), int(geom.width), int(geom.height)

    return _call(run, None, 'get_abs_geometry')


def get_frame_extents(wid):
    """``(left, right, top, bottom)`` decoration thickness. Zeros when undecorated."""
    value = get_property(wid, '_NET_FRAME_EXTENTS')
    if not value or len(value) < 4:
        return 0, 0, 0, 0
    try:
        return int(value[0]), int(value[1]), int(value[2]), int(value[3])
    except (TypeError, ValueError):
        return 0, 0, 0, 0


def get_wm_state(wid):
    """ICCCM ``WM_STATE``, or ``None`` when the property is absent (not a managed client)."""
    value = get_property(wid, 'WM_STATE')
    if not value:
        return None
    try:
        return int(value[0])
    except (TypeError, ValueError, IndexError):
        return None


def is_viewable(wid):
    def run(d):
        import Xlib.X
        return _window(d, wid).get_attributes().map_state == Xlib.X.IsViewable

    return _call(run, False, 'is_viewable') or False


def is_minimized(wid):
    """True when the window is iconified, hidden, or unmapped.

    Distinct from :func:`is_active` on purpose. Upstream's ``visible`` is a *focus*
    predicate [V15]; minimized belongs in ``pos_valid`` and in the capture layer's error.
    Checked three ways because WMs disagree: ``_NET_WM_STATE_HIDDEN`` is what KWin and
    Mutter set, ``WM_STATE == IconicState`` is the ICCCM answer, and an unmapped window is
    not capturable whatever it calls itself.
    """
    if not wid:
        return False
    hidden = atom('_NET_WM_STATE_HIDDEN')
    states = get_property(wid, '_NET_WM_STATE') or []
    if hidden and hidden in [int(s) for s in states]:
        return True
    state = get_wm_state(wid)
    if state == ICONIC_STATE:
        return True
    return not is_viewable(wid)


# --- focus -----------------------------------------------------------------------------

def get_active_window():
    """``_NET_ACTIVE_WINDOW``, or 0. The WM's answer to "what has input focus"."""
    value = _call(lambda d: _prop(d, d.screen().root.id, '_NET_ACTIVE_WINDOW'), None, 'active_window')
    if not value:
        return 0
    try:
        return int(value[0])
    except (TypeError, ValueError, IndexError):
        return 0


def get_focus_toplevel():
    """``XGetInputFocus`` walked up to the toplevel, for WMs that do not set EWMH focus.

    A reparenting WM hands focus to a frame or an input-only child, so the raw id rarely
    equals the client window; walking to the child of the root is what makes it comparable.
    """

    def run(d):
        import Xlib.X
        focus = d.get_input_focus().focus
        if focus in (Xlib.X.PointerRoot, Xlib.X.NONE, 0) or isinstance(focus, int):
            return 0
        root_id = d.screen().root.id
        wid = focus.id
        for _ in range(16):
            if not wid or wid == root_id:
                return 0
            tree = _window(d, wid).query_tree()
            if tree.parent is None or tree.parent == 0:
                return 0
            parent_id = tree.parent if isinstance(tree.parent, int) else tree.parent.id
            if parent_id == root_id:
                return int(wid)
            wid = parent_id
        return 0

    return _call(run, 0, 'focus_toplevel') or 0


def is_active(wid):
    """True when ``wid`` currently holds input focus. The Linux ``is_foreground_window``."""
    if not wid:
        return False
    if get_active_window() == wid:
        return True
    return get_focus_toplevel() == wid


# --- monitors --------------------------------------------------------------------------

def get_monitors():
    """Monitor rectangles as ``(left, top, right, bottom)``, matching ``EnumDisplayMonitors``.

    RandR ``GetMonitors`` first (it reports the logical monitors a user recognises, and is
    what a multi-head Xwayland exposes), then the CRTC list, then the screen as one
    rectangle so a headless or minimal server still yields something usable.
    """

    def run(d):
        root = d.screen().root
        try:
            monitors = root.xrandr_get_monitors().monitors
            rects = [(int(m.x), int(m.y), int(m.x) + int(m.width_in_pixels),
                      int(m.y) + int(m.height_in_pixels)) for m in monitors
                     if m.width_in_pixels > 0 and m.height_in_pixels > 0]
            if rects:
                return rects
        except Exception as e:
            logger.debug(f'xrandr_get_monitors failed, falling back to crtcs: {e}')
        try:
            resources = root.xrandr_get_screen_resources()
            rects = []
            for crtc in resources.crtcs:
                info = d.xrandr_get_crtc_info(crtc, resources.config_timestamp)
                if info.width > 0 and info.height > 0:
                    rects.append((int(info.x), int(info.y),
                                  int(info.x) + int(info.width), int(info.y) + int(info.height)))
            if rects:
                return rects
        except Exception as e:
            logger.debug(f'xrandr crtc enumeration failed, falling back to the screen: {e}')
        screen = d.screen()
        return [(0, 0, int(screen.width_in_pixels), int(screen.height_in_pixels))]

    return _call(run, [], 'get_monitors') or []


def monitor_for(x, y, width, height):
    """The monitor rectangle a window sits on -- largest overlap wins, else the first."""
    monitors = get_monitors()
    if not monitors:
        return None
    best, best_area = monitors[0], -1
    for left, top, right, bottom in monitors:
        overlap_w = min(x + width, right) - max(x, left)
        overlap_h = min(y + height, bottom) - max(y, top)
        area = max(overlap_w, 0) * max(overlap_h, 0)
        if area > best_area:
            best, best_area = (left, top, right, bottom), area
    return best


# --- actions ---------------------------------------------------------------------------

def activate(wid, timeout=0.5):
    """Raise and focus. False if the WM refused -- it must never raise [see Phase 2].

    KDE and GNOME both apply focus-stealing prevention, so a refusal is expected rather
    than exceptional; upstream's ``bring_to_front`` already treats False as recoverable.

    Mapping first is the ICCCM way to de-iconify (4.1.4: a client returns a window to the
    normal state with ``MapWindow``), and it stands in for upstream's
    ``ShowWindow(SW_RESTORE)``. It is a no-op on an already-mapped window.

    The return value is **measured, not assumed**. All three requests issued here --
    ``MapWindow``, the ``_NET_ACTIVE_WINDOW`` client message, and ``ConfigureWindow`` --
    carry no reply, so a WM that ignores them is indistinguishable from one that obeyed
    them until focus is read back. Errors on replyless requests reach ``_on_async_error``,
    never ``_call``, so returning True after ``sync()`` reported success even for a window
    id that does not exist. Poll ``is_active`` for ``timeout`` seconds instead and return
    that. Callers that only want the de-iconify half get it either way: the mapping is
    already done by the time this returns.
    """
    if not wid:
        return False

    def run(d):
        import Xlib.X
        import Xlib.protocol.event
        win = _window(d, wid)
        root = d.screen().root
        win.map()
        event = Xlib.protocol.event.ClientMessage(
            window=win,
            client_type=d.get_atom('_NET_ACTIVE_WINDOW'),
            data=(32, [1, Xlib.X.CurrentTime, 0, 0, 0]),  # source 1 == an application
        )
        root.send_event(event, event_mask=Xlib.X.SubstructureRedirectMask | Xlib.X.SubstructureNotifyMask)
        win.configure(stack_mode=Xlib.X.Above)
        d.sync()
        return True

    if not _call(run, False, 'activate'):
        return False
    deadline = time.time() + max(timeout, 0)
    while True:
        if is_active(wid):
            return True
        if time.time() >= deadline:
            logger.debug(f'activate {wid}: the window manager did not grant focus within {timeout}s')
            return False
        time.sleep(0.05)


def resize(wid, width, height, x=None, y=None):
    """Resize, and optionally move. False when the window is gone; the WM may still clamp.

    ``ConfigureWindow`` is replyless, so this cannot report a WM *refusal* -- the shape
    ``activate()`` had, and here the caller is what settles it: ``resize_window`` polls the
    real geometry for up to 5 seconds and so is never fooled by an optimistic True. What
    the caller cannot afford is paying that whole 5 seconds to discover the window never
    existed, which is what happened while this returned True for any id at all
    (``resize_window(0x7fffffff, 500, 300)`` -> False in 5.04s).

    ``get_attributes`` *is* reply-bearing, so asking for it first turns a dead window into
    a synchronous ``BadWindow`` that ``_call`` maps to False, in the same round trip and
    the same lock. A live window whose WM then ignores or clamps the request still returns
    True: that is the honest answer for a replyless request, and reading the geometry back
    is the caller's job.
    """
    if not wid or width <= 0 or height <= 0:
        return False

    def run(d):
        win = _window(d, wid)
        win.get_attributes()
        if x is None or y is None:
            win.configure(width=int(width), height=int(height))
        else:
            win.configure(x=int(x), y=int(y), width=int(width), height=int(height))
        d.sync()
        return True

    return _call(run, False, 'resize') or False
