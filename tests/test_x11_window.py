"""Phase 2 of the Linux port: the X11 window layer.

Three kinds of test live here:

* **Contract tests** -- ``find_hwnd`` and ``get_window_bounds`` are consumed positionally by
  ``DeviceManager`` and ``HwndWindow``, so their tuple shapes are load-bearing. Two of the
  assertions guard bugs the porting plan shipped and had to correct: ``real_width`` /
  ``real_height`` must be the window's size rather than 0 [V18], and ``visible`` must mean
  *foreground* rather than *mapped* [V15].
* **A drift gate** over ``X11Window``'s copy of ``HwndWindow.__init__``. The class inherits
  everything pure and overrides only the Win32-bound methods, but the constructor is a copy,
  so upstream adding an attribute would leave the Linux class silently short of it.
* **Live X11 tests**, which drive a real window through the real server: map, focus,
  iconify, resize, destroy. They skip when there is no display. Under a bare X server (CI's
  Xvfb) the window-manager-dependent ones skip too, because iconify and activation are
  requests *to a WM*.
"""

import ast
import itertools
import os
import pathlib
import sys
import threading
import time
import types
import unittest
import unittest.mock

REPO = pathlib.Path(__file__).resolve().parent.parent

skip_on_windows = unittest.skipIf(sys.platform == 'win32',
                                  'the Linux window layer is inert on Windows')


class FakeWindow:
    """One entry in the fake X server: everything the window layer reads about a window."""

    def __init__(self, wid, pid=1000, name='', geometry=(0, 0, 1920, 1080),
                 frame=(0, 0, 0, 0), wm_state=1, minimized=False, exists=True):
        self.wid = wid
        self.pid = pid
        self.name = name
        self.geometry = geometry
        self.frame = frame
        self.wm_state = wm_state
        self.minimized = minimized
        self.exists = exists


class FakeX11:
    """A stand-in for `ok.compat.x11` with the same surface the window layer uses."""

    WITHDRAWN_STATE = 0
    NORMAL_STATE = 1
    ICONIC_STATE = 3

    def __init__(self, windows=(), active=0, monitors=((0, 0, 1920, 1080),)):
        self.windows = {w.wid: w for w in windows}
        self.active = active
        self._monitors = [tuple(m) for m in monitors]
        self.activated = []
        self.resized = []

    def _get(self, wid):
        window = self.windows.get(wid)
        return window if window is not None and window.exists else None

    def list_clients(self):
        return [w.wid for w in self.windows.values() if w.exists]

    def exists(self, wid):
        return self._get(wid) is not None

    def get_pid(self, wid):
        window = self._get(wid)
        return window.pid if window else 0

    def get_name(self, wid):
        window = self._get(wid)
        return window.name if window else ''

    def get_abs_geometry(self, wid):
        window = self._get(wid)
        return window.geometry if window else None

    def get_frame_extents(self, wid):
        window = self._get(wid)
        return window.frame if window else (0, 0, 0, 0)

    def get_wm_state(self, wid):
        window = self._get(wid)
        return window.wm_state if window else None

    def is_viewable(self, wid):
        window = self._get(wid)
        return bool(window) and not window.minimized

    def is_minimized(self, wid):
        window = self._get(wid)
        return bool(window) and window.minimized

    def is_active(self, wid):
        return bool(wid) and self.active == wid

    def get_monitors(self):
        return list(self._monitors)

    def monitor_for(self, x, y, width, height):
        return self._monitors[0] if self._monitors else None

    def activate(self, wid):
        self.activated.append(wid)
        return self.exists(wid)

    def resize(self, wid, width, height, x=None, y=None):
        """Sizes and positions the *client*, like `x11.resize`.

        The coordinates are applied the way a reparenting WM applies them under the
        default NorthWest gravity: they place the frame, so the client lands `top`/`left`
        inside it. Without that, a fake cannot tell centring the window rect from
        centring the client.
        """
        window = self._get(wid)
        if not window:
            return False
        self.resized.append((wid, width, height, x, y))
        frame_left, _, frame_top, _ = window.frame
        window.geometry = (x + frame_left if x is not None else window.geometry[0],
                           y + frame_top if y is not None else window.geometry[1], width, height)
        return True


@skip_on_windows
class TestFindHwnd(unittest.TestCase):
    """`find_hwnd` is consumed positionally in three places; the tuple shape is the API."""

    GAME = ['Client-Win64-Shipping.exe']

    def run_find(self, fake, candidates, **kwargs):
        """Call the Linux find_hwnd against a fake server and a fixed pid -> exe map."""
        from ok.compat import window_x11

        def fake_candidates(pid):
            return candidates.get(pid, ([], []))

        with unittest.mock.patch.object(window_x11, 'x11', fake), \
                unittest.mock.patch.object(window_x11, '_exe_candidates', fake_candidates):
            return window_x11.find_hwnd(kwargs.pop('title', None), kwargs.pop('exe_names', self.GAME),
                                        kwargs.pop('frame_width', 0), kwargs.pop('frame_height', 0), **kwargs)

    def wine_candidates(self, pid=4242):
        return {pid: ([('Client-Win64-Shipping.exe', r'Z:\games\WW\Client-Win64-Shipping.exe')],
                      [r'Z:\games\WW\Client-Win64-Shipping.exe', '-nohmd'])}

    def test_returns_the_window_size_as_real_width_and_height(self):
        """[V18] Zeros here give DeviceManager a 0x0 device and freeze change detection."""
        fake = FakeX11([FakeWindow(0x1400001, pid=4242, name='Wuthering Waves', geometry=(0, 0, 2560, 1440))])

        name, hwnd, full_path, off_x, off_y, real_width, real_height, hwnds = self.run_find(
            fake, self.wine_candidates())

        self.assertEqual('Wuthering Waves', name)
        self.assertEqual(0x1400001, hwnd)
        self.assertEqual(r'Z:\games\WW\Client-Win64-Shipping.exe', full_path)
        self.assertEqual((0, 0), (off_x, off_y))
        self.assertEqual((2560, 1440), (real_width, real_height))
        self.assertEqual([], hwnds, 'Wine gives one X toplevel, so there are no child hwnds to report')

    def test_no_match_returns_the_upstream_empty_shape(self):
        fake = FakeX11([FakeWindow(0x1400001, pid=7, name='Something else')])

        self.assertEqual((None, 0, None, 0, 0, 0, 0, []),
                         self.run_find(fake, {7: ([('firefox', '/usr/bin/firefox')], ['/usr/bin/firefox'])}))

    def test_no_title_and_no_exe_names_returns_the_empty_shape(self):
        self.assertEqual((None, 0, None, 0, 0, 0, 0, []),
                         self.run_find(FakeX11(), {}, title=None, exe_names=None))

    def test_ignores_win32_class_names(self):
        """`UnrealWindow` exists only inside Wine; every Proton window is `steam_proton`."""
        fake = FakeX11([FakeWindow(0x1400001, pid=4242, name='Wuthering Waves')])

        _, hwnd, _, _, _, _, _, _ = self.run_find(fake, self.wine_candidates(),
                                                  class_name='UnrealWindow',
                                                  top_hwnd_class=['CLoginDlg_P_'])

        self.assertEqual(0x1400001, hwnd)

    def test_skips_wines_tiny_helper_toplevels(self):
        """Wine's 1x1 `Default IME` windows share the game's pid and must not win the tie."""
        fake = FakeX11([
            FakeWindow(0x1400001, pid=4242, name='Default IME', geometry=(0, 0, 1, 1)),
            FakeWindow(0x1400002, pid=4242, name='Wuthering Waves', geometry=(0, 0, 1920, 1080)),
        ])

        _, hwnd, _, _, _, width, height, _ = self.run_find(fake, self.wine_candidates())

        self.assertEqual(0x1400002, hwnd)
        self.assertEqual((1920, 1080), (width, height))

    def test_skips_withdrawn_windows(self):
        fake = FakeX11([FakeWindow(0x1400001, pid=4242, name='Wuthering Waves', wm_state=0)])

        self.assertEqual(0, self.run_find(fake, self.wine_candidates())[1])

    def test_selected_hwnd_beats_the_biggest_window(self):
        fake = FakeX11([
            FakeWindow(0x1400001, pid=4242, name='small', geometry=(0, 0, 800, 600)),
            FakeWindow(0x1400002, pid=4242, name='big', geometry=(0, 0, 2560, 1440)),
        ])

        self.assertEqual(0x1400001, self.run_find(fake, self.wine_candidates(), selected_hwnd=0x1400001)[1])

    def test_last_hwnd_is_sticky_within_ten_percent(self):
        fake = FakeX11([
            FakeWindow(0x1400001, pid=4242, name='previous', geometry=(0, 0, 1920, 1080)),
            FakeWindow(0x1400002, pid=4242, name='barely bigger', geometry=(0, 0, 1930, 1090)),
        ])

        self.assertEqual(0x1400001, self.run_find(fake, self.wine_candidates(), last_hwnd=0x1400001)[1])

    def test_title_filters_by_string_and_by_regex(self):
        import re
        fake = FakeX11([FakeWindow(0x1400001, pid=4242, name='Wuthering Waves')])
        candidates = self.wine_candidates()

        self.assertEqual(0x1400001, self.run_find(fake, candidates, title='Wuthering Waves', exe_names=None)[1])
        self.assertEqual(0, self.run_find(fake, candidates, title='Genshin', exe_names=None)[1])
        self.assertEqual(0x1400001, self.run_find(fake, candidates, title=re.compile('Wuther'), exe_names=None)[1])

    def test_matches_the_exe_anywhere_in_a_wine_command_line(self):
        """The game's name is a command-line argument, never `/proc/<pid>/exe`."""
        fake = FakeX11([FakeWindow(0x1400001, pid=4242, name='Wuthering Waves')])
        candidates = {4242: ([('Client-Win64-Shipping.exe', r'Z:\games\Client-Win64-Shipping.exe'),
                              ('start.exe', r'C:\windows\system32\start.exe')],
                             ['/usr/bin/wine', r'C:\windows\system32\start.exe', '/exec',
                              r'Z:\games\Client-Win64-Shipping.exe'])}

        name, hwnd, full_path = self.run_find(fake, candidates)[:3]

        self.assertEqual(0x1400001, hwnd)
        self.assertEqual(r'Z:\games\Client-Win64-Shipping.exe', full_path)

    def test_a_miss_says_why_once_rather_than_five_times_a_second(self):
        """`(None, 0, ...)` is the same answer for "not running" and for "running, but its
        `_NET_WM_PID` is a pid this process cannot see" -- which is what a pressure-vessel
        PID namespace would look like [GATE-1b]. The reasons are logged, and rate-limited
        because this runs on the 0.2s poll thread."""
        from ok.compat import window_x11
        fake = FakeX11([FakeWindow(0x1400001, pid=0, name='Wuthering Waves'),
                        FakeWindow(0x1400002, pid=4242, name='Steam')])

        with unittest.mock.patch.object(window_x11, 'x11', fake), \
                unittest.mock.patch.object(window_x11, '_exe_candidates',
                                           return_value=([('steam.exe', 'steam.exe')], [])), \
                unittest.mock.patch.object(window_x11, '_last_no_match_log', 0), \
                unittest.mock.patch.object(window_x11.logger, 'info') as info:
            for _ in range(5):
                self.assertEqual(0, window_x11.find_hwnd(None, ['Client-Win64-Shipping.exe'], 0, 0)[1])

        self.assertEqual(1, info.call_count, 'the miss report must be rate-limited')
        message = info.call_args[0][0]
        self.assertIn('matched none of 2 toplevel windows', message)
        self.assertIn('no _NET_WM_PID', message)
        self.assertIn('does not match', message)

    def test_an_unresolvable_pid_is_reported_once_not_twice(self):
        """[P2-12a] The pressure-vessel shape [GATE-1b] is the diagnosis; the generic
        `does not match` line for the same window is noise that reads like another one."""
        from ok.compat import window_x11
        fake = FakeX11([FakeWindow(0x1400001, pid=4242, name='Ghost')])

        with unittest.mock.patch.object(window_x11, 'x11', fake), \
                unittest.mock.patch.object(window_x11, '_exe_candidates', return_value=([], [])), \
                unittest.mock.patch.object(window_x11, '_last_no_match_log', 0), \
                unittest.mock.patch.object(window_x11.logger, 'info') as info:
            self.assertEqual(0, window_x11.find_hwnd(None, ['game.exe'], 0, 0)[1])

        message = info.call_args[0][0]
        self.assertIn('pid 4242 is not resolvable in /proc', message)
        self.assertNotIn('does not match', message)
        self.assertEqual(1, message.count('20971521'), 'one window, one reject line')

    def test_an_unresolvable_pid_still_matches_when_no_exe_names_are_given(self):
        """[P2-12a] The skip must not change matching: with `exe_names` unset a window
        whose pid is invisible in /proc is still a candidate, with an empty path. A title
        is required because `find_hwnd` returns a miss outright when both filters are
        None (`window_x11.py:296`)."""
        from ok.compat import window_x11
        fake = FakeX11([FakeWindow(0x1400001, pid=4242, name='Ghost', geometry=(0, 0, 800, 600))])

        with unittest.mock.patch.object(window_x11, 'x11', fake), \
                unittest.mock.patch.object(window_x11, '_exe_candidates', return_value=([], [])):
            name, hwnd, full_path = window_x11.find_hwnd('Ghost', None, 0, 0)[:3]

        self.assertEqual(0x1400001, hwnd)
        self.assertEqual('Ghost', name)
        self.assertEqual('', full_path)

    def test_a_title_only_miss_says_which_title_did_not_match(self):
        """[P2-12b] Every window filtered by title left `rejects` empty, so the message
        ended in a dangling `: `."""
        from ok.compat import window_x11
        fake = FakeX11([FakeWindow(0x1400001, pid=4242, name='Other')])

        with unittest.mock.patch.object(window_x11, 'x11', fake), \
                unittest.mock.patch.object(window_x11, '_exe_candidates',
                                           return_value=([('game.exe', '/g/game.exe')], [])), \
                unittest.mock.patch.object(window_x11, '_last_no_match_log', 0), \
                unittest.mock.patch.object(window_x11.logger, 'info') as info:
            self.assertEqual(0, window_x11.find_hwnd('Wuthering Waves', None, 0, 0)[1])

        message = info.call_args[0][0]
        self.assertFalse(message.endswith(': '), 'the reason list must never be empty')
        self.assertIn("title does not match 'Wuthering Waves'", message)

    def test_player_id_filters_on_the_command_line(self):
        fake = FakeX11([FakeWindow(0x1400001, pid=4242, name='emulator')])
        candidates = {4242: ([('dnplayer.exe', '/games/dnplayer.exe')], ['/games/dnplayer.exe', '3'])}

        self.assertEqual(0x1400001, self.run_find(fake, candidates, exe_names=['dnplayer.exe'], player_id=3)[1])
        self.assertEqual(0, self.run_find(fake, candidates, exe_names=['dnplayer.exe'], player_id=5)[1])


@skip_on_windows
class TestFocusClientWindow(unittest.TestCase):
    """[P2-14] `is_active` must see the client, not the reparenting WM's frame.

    Measured on a nested `Xwayland :9` with the client reparented into an
    override-redirect frame and the input focus set on the client: `get_focus_toplevel`
    returned the frame, so `is_active(client)` was False while `XGetInputFocus` named the
    client, `is_foreground_window` was False for a focused game, and `x11.activate` spent
    its whole 0.5s timeout to report a refusal of focus it had been granted. Unit-tested
    rather than driven live because the fallback only runs on a WM that publishes no
    `_NET_ACTIVE_WINDOW`, and this desktop (KWin) and CI (Xvfb, no WM) are neither.
    """

    class _Window:
        def __init__(self, wid, parent=None, wm_state=False, children=(), raises=False):
            self.id = wid
            self.parent = parent
            self.wm_state = wm_state
            self.children = list(children)
            self.raises = raises

    class _Display:
        """The three calls `_client_window` makes: get_atom, create_resource_object, query_tree."""

        def __init__(self, windows):
            self.windows = {w.id: w for w in windows}

        def get_atom(self, name):
            return 39 if name == 'WM_STATE' else 1

        def create_resource_object(self, kind, wid):
            # The root and anything outside the fixture behave as a bare window.
            window = self.windows.get(wid) or TestFocusClientWindow._Window(wid)
            display = self

            class _Resource:
                id = wid

                def get_full_property(self, atom, kind_):
                    if window.raises:
                        raise RuntimeError('BadWindow: it went away mid-walk')
                    # SimpleNamespace, not Mock: `parent` is a reserved Mock kwarg and a
                    # `Mock(parent=...)` silently hands back the wrong object below.
                    return types.SimpleNamespace(value=[1, 0]) if window.wm_state else None

                def query_tree(self):
                    if window.raises:
                        raise RuntimeError('BadWindow: it went away mid-walk')
                    return types.SimpleNamespace(
                        parent=None if window.parent is None else display.create_resource_object('window', window.parent),
                        children=[display.create_resource_object('window', c) for c in window.children])

            return _Resource()

    ROOT = 0x100

    def test_focus_on_an_input_child_resolves_to_the_client(self):
        from ok.compat import x11
        display = self._Display([
            self._Window(0x1400002, parent=0x1400001),
            self._Window(0x1400001, parent=self.ROOT, wm_state=True, children=[0x1400002]),
        ])

        self.assertEqual(0x1400001, x11._client_window(display, 0x1400002, self.ROOT))

    def test_a_reparented_client_is_not_reported_as_its_frame(self):
        """The regression: the frame is the root's child, so the old walk returned it."""
        from ok.compat import x11
        display = self._Display([
            self._Window(0x1400001, parent=0x2000001, wm_state=True),
            self._Window(0x2000001, parent=self.ROOT, children=[0x1400001]),
        ])

        self.assertEqual(0x1400001, x11._client_window(display, 0x1400001, self.ROOT))

    def test_a_wm_that_focuses_the_frame_still_resolves_to_the_client(self):
        """One level of descent: the frame carries no WM_STATE, its child does."""
        from ok.compat import x11
        display = self._Display([
            self._Window(0x2000001, parent=self.ROOT, children=[0x1400001]),
            self._Window(0x1400001, parent=0x2000001, wm_state=True),
        ])

        self.assertEqual(0x1400001, x11._client_window(display, 0x2000001, self.ROOT))

    def test_with_no_wm_state_anywhere_the_root_child_is_the_answer(self):
        """A bare X server with no window manager, which is also what CI runs under."""
        from ok.compat import x11
        display = self._Display([
            self._Window(0x1400002, parent=0x1400001),
            self._Window(0x1400001, parent=self.ROOT, children=[0x1400002]),
        ])

        self.assertEqual(0x1400001, x11._client_window(display, 0x1400002, self.ROOT))

    def test_a_window_that_dies_mid_walk_is_zero_not_an_exception(self):
        from ok.compat import x11
        display = self._Display([self._Window(0x1400001, parent=self.ROOT, raises=True)])

        self.assertEqual(0, x11._client_window(display, 0x1400001, self.ROOT))


@skip_on_windows
class TestFrameSkip(unittest.TestCase):
    """[P2-11] `list_clients`' source 3 must not re-add a reparenting WM's frames.

    Measured on a nested Xwayland with a minimal reparenting WM: keeping them cost
    `find_hwnd` 5.28 ms/call against 3.17 ms under a non-reparenting WM, and added one
    `no _NET_WM_PID` line per managed window to P2-6's rejection message. The predicate
    is unit-tested rather than driven live because reproducing the shape needs a
    reparenting WM, and neither this machine's session (kwin_wayland, which does not
    reparent) nor CI (Xvfb, no WM at all) is one.
    """

    class _Child:
        def __init__(self, wid, children=(), raises=False):
            self.id = wid
            self._children = list(children)
            self._raises = raises

        def query_tree(self):
            if self._raises:
                raise RuntimeError('BadWindow: it went away between the two calls')
            return unittest.mock.Mock(children=self._children)

    def test_a_frame_around_a_client_we_already_have_is_a_frame(self):
        from ok.compat import x11
        client = self._Child(0x1400001)
        frame = self._Child(0x2000001, children=[client])

        self.assertTrue(x11._frames_a_known_client(frame, {0x1400001}))

    def test_an_override_redirect_toplevel_is_not_a_frame(self):
        """P2-7's window: nothing else in the tree holds it, which is why source 3 exists.
        A predicate that dropped unnamed or pid-less windows instead would delete it."""
        from ok.compat import x11
        unmanaged = self._Child(0x1400009, children=[])

        self.assertFalse(x11._frames_a_known_client(unmanaged, {0x1400001, 0x1400002}))

    def test_a_toplevel_whose_children_are_all_unknown_is_not_a_frame(self):
        """A client's own sub-windows are not toplevels and are never in `seen`; a bare
        X server with no WM at all must keep every one of the root's children."""
        from ok.compat import x11
        toplevel = self._Child(0x1400003, children=[self._Child(0x1400004),
                                                    self._Child(0x1400005)])

        self.assertFalse(x11._frames_a_known_client(toplevel, {0x1400001}))

    def test_a_window_that_dies_mid_walk_is_not_a_frame(self):
        """Enumeration races window destruction; nothing in `ok.compat.x11` may raise."""
        from ok.compat import x11
        gone = self._Child(0x1400006, raises=True)

        self.assertFalse(x11._frames_a_known_client(gone, {0x1400001}))


@skip_on_windows
class TestGetWindowBounds(unittest.TestCase):

    def test_folds_the_frame_back_into_the_window_rect(self):
        """`try_resize_to` derives the border and title height from window minus client."""
        from ok.compat import window_x11
        fake = FakeX11([FakeWindow(0x1400001, geometry=(100, 200, 1280, 720), frame=(4, 4, 28, 4))])

        with unittest.mock.patch.object(window_x11, 'x11', fake):
            x, y, window_width, window_height, width, height, scaling = window_x11.get_window_bounds(0x1400001)

        self.assertEqual((100, 200), (x, y))
        self.assertEqual((1288, 752), (window_width, window_height))
        self.assertEqual((1280, 720), (width, height))
        self.assertEqual(1.0, scaling)

    def test_returns_the_upstream_fallback_when_the_window_is_gone(self):
        from ok.compat import window_x11

        with unittest.mock.patch.object(window_x11, 'x11', FakeX11()):
            self.assertEqual((0, 0, 0, 0, 0, 0, 1), window_x11.get_window_bounds(0x1400001))


@skip_on_windows
class TestResizeWindow(unittest.TestCase):
    """`resize_window`'s `width`/`height` are the WINDOW rect, decorations included.

    Both callers pass outer dimensions -- `try_resize_to` adds the border and title-bar
    height to the target resolution, `start_controller`'s re-centre path passes
    `window_width`/`window_height` -- because the Windows body calls `SetWindowPos`, which
    sizes the window rect, and settles against `GetWindowRect`. Sizing the client to those
    numbers made `try_resize_to` overshoot by a title bar and then report failure, and made
    the re-centre path grow the window by the frame extents on every single call.
    """

    FRAME = (4, 4, 28, 0)     # (left, right, top, bottom), a KWin-style decoration

    def _window(self, geometry=(100, 200, 1280, 720)):
        return FakeWindow(0x1400001, geometry=geometry, frame=self.FRAME)

    def test_the_client_is_sized_to_the_request_minus_the_frame(self):
        from ok.compat import window_x11
        fake = FakeX11([self._window()], monitors=((0, 0, 1920, 1080),))

        with unittest.mock.patch.object(window_x11, 'x11', fake):
            self.assertTrue(window_x11.resize_window(0x1400001, 1288, 748))
            bounds = window_x11.get_window_bounds(0x1400001)

        self.assertEqual((1280, 720), fake.resized[-1][1:3])
        self.assertEqual((1288, 748), bounds[2:4], 'the window rect must equal the request')
        self.assertEqual((1280, 720), bounds[4:6])

    def test_repeated_recentring_does_not_grow_the_window(self):
        """`start_controller` calls this with `window_width`/`window_height` every poll
        in which the position is invalid. Sizing the client to them added the frame
        extents each time, monotonically, until the WM clamped it."""
        from ok.compat import window_x11
        fake = FakeX11([self._window()], monitors=((0, 0, 1920, 1080),))

        with unittest.mock.patch.object(window_x11, 'x11', fake):
            for _ in range(5):
                bounds = window_x11.get_window_bounds(0x1400001)
                window_x11.resize_window(0x1400001, bounds[2], bounds[3])
            final = window_x11.get_window_bounds(0x1400001)

        self.assertEqual((1288, 748), final[2:4])
        self.assertEqual((1280, 720), final[4:6])

    def test_try_resize_to_hits_the_requested_resolution_and_reports_success(self):
        """The end-to-end shape of the bug: content one title bar too tall, and
        `resize hwnd failed` logged even though the window manager obeyed."""
        from ok.compat import window_x11
        fake = FakeX11([self._window(geometry=(100, 200, 1000, 600))], monitors=((0, 0, 1920, 1080),))
        found = ('Wuthering Waves', 0x1400001, r'Z:\WW\Client-Win64-Shipping.exe', 0, 0, 1000, 600, [])

        window, patches = make_x11_window(fake, found, monitors=[(0, 0, 1920, 1080)])
        try:
            # `Auto Resize Game Window` defaults to on; make_x11_window turns every option off.
            window.global_config.get_config.return_value.get.return_value = True
            self.assertTrue(window.try_resize_to([(1280, 720)]))
            with unittest.mock.patch.object(window_x11, 'x11', fake):
                bounds = window_x11.get_window_bounds(0x1400001)
        finally:
            for patch in reversed(patches):
                patch.stop()

        self.assertEqual((1280, 720), bounds[4:6], 'the content must be the requested resolution')
        self.assertEqual((1288, 748), (window.window_width, window.window_height))

    def test_the_undecorated_path_is_unchanged(self):
        from ok.compat import window_x11
        fake = FakeX11([FakeWindow(0x1400001, geometry=(0, 0, 800, 600))], monitors=((0, 0, 1920, 1080),))

        with unittest.mock.patch.object(window_x11, 'x11', fake):
            self.assertTrue(window_x11.resize_window(0x1400001, 1280, 720))

        self.assertEqual((0x1400001, 1280, 720, 320, 180), fake.resized[-1])

    def test_a_window_that_never_settles_reports_failure(self):
        from ok.compat import window_x11
        fake = FakeX11([self._window()], monitors=((0, 0, 1920, 1080),))
        fake.resize = lambda *args, **kwargs: True      # the WM ignores the request

        with unittest.mock.patch.object(window_x11, 'x11', fake), \
                unittest.mock.patch.object(window_x11.time, 'sleep', lambda _: None), \
                unittest.mock.patch.object(window_x11.time, 'time', itertools.count(0, 2).__next__):
            self.assertFalse(window_x11.resize_window(0x1400001, 1000, 600))


def make_x11_window(fake_x11, find_hwnd_result, capture_method=None, executor=None, monitors=None,
                    frame=(0, 0)):
    """Construct a real `X11Window` against a fake server, without its polling thread.

    `threading.Thread` is patched rather than the constructor being bypassed, so the test
    exercises upstream's actual attribute list -- which is the point of the drift gate below.
    """
    from ok.compat import window_x11
    from ok.device.capture_methods import x11_window

    device_manager = unittest.mock.Mock()
    device_manager.config = {'selected_exe': ['Client-Win64-Shipping.exe'], 'selected_hwnd': 0}
    device_manager.capture_method = capture_method
    device_manager.executor = executor
    device_manager.get_preferred_device.return_value = None

    global_config = unittest.mock.Mock()
    options = unittest.mock.Mock()
    options.get.return_value = False       # mute off, auto-resize off, exit-on-game-exit off
    global_config.get_config.return_value = options

    patches = [
        unittest.mock.patch.object(x11_window, 'x11', fake_x11),
        # `get_window_bounds` and `is_foreground_window` reach the server through
        # `ok.compat.window_x11`, not through the class, so the fake has to sit there too.
        unittest.mock.patch.object(window_x11, 'x11', fake_x11),
        unittest.mock.patch.object(x11_window, 'find_hwnd', return_value=find_hwnd_result),
        unittest.mock.patch.object(x11_window, 'communicate', unittest.mock.Mock()),
        unittest.mock.patch.object(x11_window, 'set_mute_state'),
        unittest.mock.patch.object(x11_window, 'get_monitors_bounds',
                                   return_value=list(monitors or [(0, 0, 1920, 1080)])),
        unittest.mock.patch.object(x11_window.threading, 'Thread'),
    ]
    for patch in patches:
        patch.start()
    window = x11_window.X11Window(threading.Event(), None, exe_name='Client-Win64-Shipping.exe',
                                  frame_width=frame[0], frame_height=frame[1],
                                  global_config=global_config, device_manager=device_manager)
    return window, patches


@skip_on_windows
class TestX11WindowSemantics(unittest.TestCase):

    def setUp(self):
        self.patches = []

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def build(self, fake, **kwargs):
        window, self.patches = make_x11_window(fake, **kwargs)
        return window

    def found(self, hwnd, width=1920, height=1080, name='Wuthering Waves'):
        return name, hwnd, r'Z:\WW\Client-Win64-Shipping.exe', 0, 0, width, height, []

    def test_visible_means_foreground_not_mapped(self):
        """[V15] A mapped-based `visible` is True for all of background play, which silently
        disables MouseResetTask's cursor pinning -- the one thing this port exists for."""
        fake = FakeX11([FakeWindow(0x1400001, geometry=(0, 0, 1920, 1080))], active=0)
        window = self.build(fake, find_hwnd_result=self.found(0x1400001))

        self.assertTrue(window.exists)
        self.assertFalse(window.visible, 'a mapped but unfocused game window is not "visible"')

        fake.active = 0x1400001
        window.do_update_window_size()
        self.assertTrue(window.visible)

    def test_pos_valid_goes_false_when_the_window_is_minimized(self):
        """An iconified X11 window keeps its geometry, so check_pos alone never notices."""
        fake = FakeX11([FakeWindow(0x1400001, geometry=(0, 0, 1920, 1080))])
        window = self.build(fake, find_hwnd_result=self.found(0x1400001))

        self.assertTrue(window.pos_valid)

        fake.windows[0x1400001].minimized = True
        window.do_update_window_size()
        self.assertFalse(window.pos_valid)

        fake.windows[0x1400001].minimized = False
        window.do_update_window_size()
        self.assertTrue(window.pos_valid)

    def test_minimizing_pauses_the_executor_once(self):
        from ok.device.capture_methods.base import BaseWindowsCaptureMethod

        executor = unittest.mock.Mock()
        executor.pause.return_value = True
        fake = FakeX11([FakeWindow(0x1400001, geometry=(0, 0, 1920, 1080))])
        window = self.build(fake, find_hwnd_result=self.found(0x1400001),
                            capture_method=BaseWindowsCaptureMethod(None), executor=executor)

        fake.windows[0x1400001].minimized = True
        window.do_update_window_size()

        executor.pause.assert_called_once()

    def test_pos_valid_goes_false_when_the_window_leaves_every_monitor(self):
        fake = FakeX11([FakeWindow(0x1400001, geometry=(0, 0, 1920, 1080))])
        window = self.build(fake, find_hwnd_result=self.found(0x1400001))
        self.assertTrue(window.pos_valid)

        fake.windows[0x1400001].geometry = (9000, 9000, 1920, 1080)
        window.do_update_window_size()

        self.assertFalse(window.pos_valid)

    def test_geometry_and_identity_track_the_server(self):
        fake = FakeX11([FakeWindow(0x1400001, geometry=(64, 32, 1600, 900), frame=(0, 0, 24, 0))])
        window = self.build(fake, find_hwnd_result=self.found(0x1400001, 1600, 900))

        self.assertEqual(0x1400001, window.hwnd)
        self.assertEqual((64, 32), (window.x, window.y))
        self.assertEqual((1600, 900), (window.width, window.height))
        self.assertEqual((1600, 924), (window.window_width, window.window_height))
        self.assertEqual((1600, 900), (window.client_width, window.client_height))
        self.assertEqual((1600, 900), (window.real_width, window.real_height))
        self.assertEqual(1.0, window.scaling)

    def test_top_hwnd_collapses_onto_the_main_window(self):
        """With no child hwnds, `get_top_window_cords` must be the identity."""
        fake = FakeX11([FakeWindow(0x1400001)])
        window = self.build(fake, find_hwnd_result=self.found(0x1400001))

        self.assertEqual(0x1400001, window.top_hwnd)
        self.assertEqual((0, 0), (window.top_offset_x, window.top_offset_y))
        self.assertEqual((10, 20), window.get_top_window_cords(10, 20))

    def test_a_letterboxed_window_crops_and_offsets_like_the_windows_original(self):
        """The capture origin comes from upstream's `get_crop_point`, fed by X11 geometry.

        `get_abs_cords` and `get_capture_origin` are inherited unchanged, but they read
        `client_*` and `width`/`height`, which the Linux poll fills in -- so the asymmetric
        crop (all of the slack goes below the content) only stays correct if those are.
        A 16:10 window showing a 16:9 frame is the case that separates them.
        """
        fake = FakeX11([FakeWindow(0x1400001, geometry=(100, 50, 1920, 1200))])
        window = self.build(fake, find_hwnd_result=self.found(0x1400001, 1920, 1200),
                            frame=(1920, 1080))

        self.assertEqual((1920, 1200), (window.client_width, window.client_height))
        self.assertEqual((1920, 1080), (window.width, window.height))
        self.assertEqual((100, 170), window.get_capture_origin())
        self.assertEqual((110, 70), window.get_abs_cords(10, 20))

    def test_game_exit_clears_the_hwnd(self):
        fake = FakeX11([FakeWindow(0x1400001)])
        window = self.build(fake, find_hwnd_result=self.found(0x1400001))
        self.assertTrue(window.exists)

        fake.windows[0x1400001].exists = False
        window.do_update_window_size()

        self.assertEqual(0, window.hwnd)
        self.assertFalse(window.visible)

    def test_bring_to_front_reports_a_refusal_instead_of_raising(self):
        fake = FakeX11([FakeWindow(0x1400001)])
        window = self.build(fake, find_hwnd_result=self.found(0x1400001))

        self.assertTrue(window.bring_to_front())
        self.assertEqual([0x1400001], fake.activated)

        fake.windows[0x1400001].exists = False
        self.assertFalse(window.bring_to_front())

    def test_hwnd_title_is_read_from_x11_and_cached(self):
        fake = FakeX11([FakeWindow(0x1400001, name='Wuthering Waves')])
        window = self.build(fake, find_hwnd_result=self.found(0x1400001))

        self.assertEqual('Wuthering Waves', window.hwnd_title)
        fake.windows[0x1400001].name = 'changed'
        self.assertEqual('Wuthering Waves', window.hwnd_title)


@skip_on_windows
class TestMute(unittest.TestCase):
    """Per-application mute is recoverable on Linux via PipeWire/PulseAudio sink inputs."""

    SINK_INPUTS = '''Sink Input #12
\tDriver: PipeWire
\tMute: no
\tProperties:
\t\tapplication.process.id = "4242"
\t\tapplication.name = "Wuthering Waves"
Sink Input #13
\tDriver: PipeWire
\tMute: yes
\tProperties:
\t\tapplication.process.id = "999"
'''

    def test_parses_pactl_output(self):
        from ok.device.capture_methods import x11_window

        self.assertEqual([('12', 4242, False), ('13', 999, True)],
                         x11_window._parse_sink_inputs(self.SINK_INPUTS))

    def test_parses_empty_output(self):
        from ok.device.capture_methods import x11_window

        self.assertEqual([], x11_window._parse_sink_inputs(''))
        self.assertEqual([], x11_window._parse_sink_inputs(None))

    def test_mutes_only_the_streams_of_the_windows_process(self):
        from ok.device.capture_methods import x11_window
        calls = []

        def fake_pactl(*args):
            calls.append(args)
            return self.SINK_INPUTS if args[:2] == ('list', 'sink-inputs') else ''

        with unittest.mock.patch.object(x11_window, 'x11', FakeX11([FakeWindow(0x1400001, pid=4242)])), \
                unittest.mock.patch.object(x11_window, '_pactl', fake_pactl):
            x11_window.set_mute_state(0x1400001, 1)

        self.assertIn(('set-sink-input-mute', '12', '1'), calls)
        self.assertNotIn(('set-sink-input-mute', '13', '1'), calls)

    def test_get_mute_state_reports_the_current_value(self):
        from ok.device.capture_methods import x11_window

        with unittest.mock.patch.object(x11_window, 'x11', FakeX11([FakeWindow(0x1400001, pid=999)])), \
                unittest.mock.patch.object(x11_window, '_pactl', return_value=self.SINK_INPUTS):
            self.assertEqual(1, x11_window.get_mute_state(0x1400001))

        with unittest.mock.patch.object(x11_window, 'x11', FakeX11([FakeWindow(0x1400001, pid=4242)])), \
                unittest.mock.patch.object(x11_window, '_pactl', return_value=self.SINK_INPUTS):
            self.assertEqual(0, x11_window.get_mute_state(0x1400001))

    # The same two streams as SINK_INPUTS, from a de_DE pactl. `pactl` translates its own
    # output, and every literal this parser matches moves: `Sink Input #` -> `Ziel-Eingabe #`,
    # `Mute:` -> `Stumm:`. zh_CN, the largest part of ok-ww's userbase, says `信宿输入 #`.
    SINK_INPUTS_DE = '''Ziel-Eingabe #12
\tTreiber: PipeWire
\tStumm: nein
\tEigenschaften:
\t\tapplication.process.id = "4242"
'''

    def test_a_localized_pactl_parses_to_nothing_which_is_why_the_env_is_pinned(self):
        from ok.device.capture_methods import x11_window

        self.assertEqual([], x11_window._parse_sink_inputs(self.SINK_INPUTS_DE))

    def test_pactl_runs_in_a_c_locale(self):
        """Silent failure otherwise: exit 0, output on stdout, and nothing parsed."""
        from ok.device.capture_methods import x11_window
        captured = {}

        def fake_run(argv, **kwargs):
            captured['argv'] = argv
            captured['env'] = kwargs.get('env')
            return unittest.mock.Mock(returncode=0, stdout=self.SINK_INPUTS, stderr='')

        with unittest.mock.patch.object(x11_window.shutil, 'which', return_value='/usr/bin/pactl'), \
                unittest.mock.patch.object(x11_window.subprocess, 'run', fake_run):
            self.assertEqual(self.SINK_INPUTS, x11_window._pactl('list', 'sink-inputs'))

        self.assertEqual(('pactl', 'list', 'sink-inputs'), captured['argv'])
        self.assertEqual('C', captured['env']['LC_ALL'])
        # LANGUAGE overrides LC_ALL for gettext, so clearing it is not optional.
        self.assertEqual('', captured['env']['LANGUAGE'])
        self.assertIn('PATH', captured['env'], 'the rest of the environment must survive')

    def test_a_stream_already_in_the_requested_state_is_not_rewritten(self):
        from ok.device.capture_methods import x11_window
        calls = []

        def fake_pactl(*args):
            calls.append(args)
            return self.SINK_INPUTS if args[:2] == ('list', 'sink-inputs') else ''

        with unittest.mock.patch.object(x11_window, 'x11', FakeX11([FakeWindow(0x1400001, pid=999)])), \
                unittest.mock.patch.object(x11_window, '_pactl', fake_pactl):
            x11_window.set_mute_state(0x1400001, 1)      # sink input 13 is already muted

        self.assertEqual([('list', 'sink-inputs')], calls)

    def test_missing_pactl_is_not_an_error(self):
        from ok.device.capture_methods import x11_window

        with unittest.mock.patch.object(x11_window, 'x11', FakeX11([FakeWindow(0x1400001, pid=4242)])), \
                unittest.mock.patch.object(x11_window.shutil, 'which', return_value=None):
            self.assertEqual(0, x11_window.get_mute_state(0x1400001))
            x11_window.set_mute_state(0x1400001, 1)


@skip_on_windows
class TestWindowTitles(unittest.TestCase):
    """`WM_NAME` is ICCCM `STRING`, i.e. Latin-1; only `_NET_WM_NAME` is UTF-8."""

    ACCENTED = 'Wuthering Wavés'

    def _get_name(self, properties):
        from ok.compat import x11

        with unittest.mock.patch.object(x11, 'get_property', lambda wid, name: properties.get(name)):
            return x11.get_name(0x1400001)

    def test_net_wm_name_is_decoded_as_utf8(self):
        self.assertEqual(self.ACCENTED, self._get_name({'_NET_WM_NAME': self.ACCENTED.encode('utf-8')}))

    def test_wm_name_is_decoded_as_latin1(self):
        self.assertEqual(self.ACCENTED, self._get_name({'WM_NAME': self.ACCENTED.encode('latin-1')}))

    def test_an_unnamed_window_is_the_empty_string(self):
        self.assertEqual('', self._get_name({}))


@skip_on_windows
class TestUtilWindowShadow(unittest.TestCase):
    """`ok/util/window.py` is the choke point; the shadow must be complete and no wider."""

    SHADOWED = ('find_hwnd', 'get_window_bounds', 'is_foreground_window', 'show_title_bar',
                'resize_window', 'find_all_visible_windows', 'get_exe_by_hwnd', 'is_window_minimized')

    def test_the_x11_implementations_are_the_ones_callers_get(self):
        from ok.util import window

        for name in self.SHADOWED:
            self.assertEqual('ok.compat.window_x11', getattr(window, name).__module__, name)

    def test_the_four_platform_neutral_contracts_are_left_alone(self):
        """[V24] Omitting any of these is an ImportError across the whole device layer."""
        from ok.util import window

        self.assertEqual(-1, window.WINDOWS_BUILD_NUMBER)
        self.assertEqual(20348, window.WGC_NO_BORDER_MIN_BUILD)
        self.assertEqual(16 / 9, window.ratio_text_to_number('16:9'))
        self.assertEqual('ok.util.window', window.find_display.__module__)
        self.assertFalse(window.windows_graphics_available())

    def test_hwnd_window_resolves_to_the_x11_class(self):
        from ok.device import capture
        from ok.device.capture_methods import x11_window

        self.assertIs(x11_window.X11Window, capture.HwndWindow)
        for name in ('check_pos', 'get_monitors_bounds', 'get_mute_state',
                     'is_window_in_screen_bounds', 'set_mute_state'):
            self.assertIs(getattr(x11_window, name), getattr(capture, name), name)

    def test_device_manager_constructs_the_x11_window(self):
        import ok.device.DeviceManager as device_manager
        from ok.device.capture_methods import x11_window

        self.assertIs(x11_window.X11Window, device_manager.HwndWindow)


@skip_on_windows
class TestUpstreamDrift(unittest.TestCase):
    """`X11Window` copies upstream's constructor; fail loudly if upstream's grows."""

    @staticmethod
    def _class_node(path, class_name):
        tree = ast.parse((REPO / path).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return node
        raise AssertionError(f'{class_name} not found in {path}')

    @staticmethod
    def _self_attributes(target):
        """`self.a`, `self.a, self.b = ...` and `self.a: int = ...` all set an attribute.

        Only the first shape was collected before, so a `self.x, self.y = x, y` added to
        upstream's `__init__` -- already idiomatic in that class, `do_update_window_size`
        uses it -- would have been invisible to the gate.
        """
        if isinstance(target, (ast.Tuple, ast.List)):
            names = set()
            for element in target.elts:
                names |= TestUpstreamDrift._self_attributes(element)
            return names
        if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == 'self'):
            return {target.attr}
        return set()

    @classmethod
    def _init_attributes(cls, node):
        init = next(n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
        names = set()
        for statement in ast.walk(init):
            targets = list(getattr(statement, 'targets', []))
            if isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
                targets.append(statement.target)
            for target in targets:
                names |= cls._self_attributes(target)
        return names

    @staticmethod
    def _win32_bound_methods(path, class_name):
        """Upstream methods that reach Win32, directly or through their own module.

        Three sources of taint, all read out of upstream's own file so the list cannot go
        stale: the `win32*`/`ctypes` modules it imports, the module-level helpers in that
        file whose bodies use them (`get_monitors_bounds`, `get_mute_state`,
        `set_mute_state`), and the `ok.util.window` contracts it imports, whose Windows
        bodies are Win32 and which `ok/compat/window_x11.py` exists to shadow.
        """
        tree = ast.parse((REPO / path).read_text(encoding='utf-8'))
        tainted = {'ctypes'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tainted |= {alias.name.split('.')[0] for alias in node.names
                            if alias.name.startswith('win32') or alias.name == 'ctypes'}
            elif isinstance(node, ast.ImportFrom) and (node.module or '') == 'ok.util.window':
                tainted |= {alias.asname or alias.name for alias in node.names}

        def references(node):
            return ({n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                    | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)})

        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and references(node) & tainted:
                tainted.add(node.name)

        cls_node = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == class_name)
        return {n.name for n in cls_node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and references(n) & tainted}

    def test_the_constructor_sets_every_attribute_upstream_sets(self):
        upstream = self._class_node('ok/device/capture_methods/hwnd_window.py', 'HwndWindow')
        linux = self._class_node('ok/device/capture_methods/x11_window.py', 'X11Window')

        missing = self._init_attributes(upstream) - self._init_attributes(linux)

        self.assertEqual(set(), missing,
                         'HwndWindow.__init__ gained attributes; mirror them in X11Window.__init__')

    def test_every_win32_bound_upstream_method_is_actually_overridden(self):
        """`hasattr` proves nothing here: X11Window *inherits* everything.

        The gate this replaces asked `hasattr(X11Window, name)` for every name in
        `vars(HwndWindow)`, which is True by definition for a subclass -- it could not
        fail, and the drift it advertised (upstream gains a Win32-calling method) would
        have landed as a silently inherited NotImplementedError at runtime with a green
        suite. Ask the question that can fail instead: every upstream method that touches
        Win32 must appear in `vars(X11Window)`, i.e. be genuinely overridden.
        """
        from ok.device.capture_methods.hwnd_window import HwndWindow
        from ok.device.capture_methods.x11_window import X11Window

        self.assertTrue(issubclass(X11Window, HwndWindow))
        win32_bound = self._win32_bound_methods('ok/device/capture_methods/hwnd_window.py', 'HwndWindow')
        self.assertIn('bring_to_front', win32_bound, 'the taint analysis stopped finding anything')

        inherited = sorted(name for name in win32_bound if name not in vars(X11Window))

        self.assertEqual([], inherited,
                         'these upstream methods call Win32 and X11Window inherits them unchanged')

    def test_the_method_gate_sees_upstream_growing_a_win32_method(self):
        """The drift the gate above claims to catch, simulated."""
        import tempfile
        source = (REPO / 'ok/device/capture_methods/hwnd_window.py').read_text(encoding='utf-8')
        marker = 'class HwndWindow:\n'
        self.assertIn(marker, source)
        source = source.replace(
            marker,
            marker + '\n    def brand_new_win32_method(self):\n        return win32gui.IsWindow(self.hwnd)\n',
            1)
        with tempfile.TemporaryDirectory() as tmp:
            drifted = pathlib.Path(tmp) / 'hwnd_window.py'
            drifted.write_text(source, encoding='utf-8')
            with unittest.mock.patch.object(sys.modules[__name__], 'REPO', pathlib.Path(tmp)):
                win32_bound = self._win32_bound_methods('hwnd_window.py', 'HwndWindow')

        self.assertIn('brand_new_win32_method', win32_bound)

    def test_the_constructor_gate_sees_tuple_and_annotated_targets(self):
        node = ast.parse('class C:\n'
                         '    def __init__(self):\n'
                         '        self.a = 1\n'
                         '        self.b, self.c = 2, 3\n'
                         '        self.d: int = 4\n').body[0]

        self.assertEqual({'a', 'b', 'c', 'd'}, self._init_attributes(node))

    def test_the_linux_modules_call_no_win32(self):
        for path in ('ok/compat/x11.py', 'ok/compat/window_x11.py', 'ok/compat/xshm.py',
                     'ok/device/capture_methods/x11_window.py',
                     'ok/device/capture_methods/x11_capture.py'):
            source = (REPO / path).read_text(encoding='utf-8')
            tree = ast.parse(source)
            imported = {node.names[0].name.split('.')[0]
                        for node in ast.walk(tree) if isinstance(node, ast.Import)}
            imported |= {(node.module or '').split('.')[0]
                         for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
            self.assertEqual(set(), {name for name in imported if name.startswith('win32') or name == 'winreg'},
                             f'{path} must not import Win32')


def _wm_present():
    """True when a window manager is running: iconify and activation are requests *to* one."""
    from ok.compat import x11
    return bool(x11.get_property(_root_id(), '_NET_SUPPORTING_WM_CHECK'))


def _root_id():
    from Xlib import display
    return display.Display().screen().root.id


@skip_on_windows
class TestLiveX11(unittest.TestCase):
    """Drive a real window through a real X server. Skipped when there is no display."""

    @classmethod
    def setUpClass(cls):
        from ok.compat import x11
        if not x11.available():
            raise unittest.SkipTest('no usable X11 display')

    def setUp(self):
        from Xlib import X, Xatom, display
        self.display = display.Display()
        screen = self.display.screen()
        self.window = screen.root.create_window(
            150, 120, 320, 240, 0, screen.root_depth, X.InputOutput, X.CopyFromParent,
            background_pixel=screen.black_pixel, event_mask=X.StructureNotifyMask)
        self.window.set_wm_name('ok-script x11 test window')
        self.window.set_wm_class('okscripttest', 'OkScriptTest')
        self.window.change_property(self.display.get_atom('_NET_WM_PID'), Xatom.CARDINAL, 32, [os.getpid()])
        self.window.map()
        self.display.sync()
        self.wid = self.window.id
        from ok.compat import x11
        # Being in the client list is not the same as being mapped: SetInputFocus and
        # XGetImage both need viewable, and the WM gets there a moment later.
        self._wait_for(lambda: self.wid in self._clients() and x11.is_viewable(self.wid))

    def tearDown(self):
        try:
            self.window.destroy()
            self.display.sync()
            self.display.close()
        except Exception:
            pass

    @staticmethod
    def _clients():
        from ok.compat import x11
        return x11.list_clients()

    @staticmethod
    def _wait_for(predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_the_window_is_discoverable_by_pid_and_geometry(self):
        from ok.compat import x11
        from ok.util import window

        self.assertIn(self.wid, self._clients())
        self.assertEqual(os.getpid(), x11.get_pid(self.wid))
        self.assertEqual('ok-script x11 test window', x11.get_name(self.wid))

        x, y, window_width, window_height, width, height, scaling = window.get_window_bounds(self.wid)
        self.assertEqual((320, 240), (width, height))
        self.assertGreaterEqual(window_width, width)
        self.assertGreaterEqual(window_height, height)
        self.assertEqual(1.0, scaling)

    def test_find_hwnd_matches_this_process(self):
        from ok.util import window
        import psutil

        exe_name = psutil.Process(os.getpid()).name()
        name, hwnd, _, off_x, off_y, real_width, real_height, hwnds = window.find_hwnd(
            'ok-script x11 test window', [exe_name], 0, 0)

        self.assertEqual(self.wid, hwnd)
        self.assertEqual('ok-script x11 test window', name)
        self.assertEqual((0, 0), (off_x, off_y))
        self.assertEqual((320, 240), (real_width, real_height))
        self.assertEqual([], hwnds)

    def test_focus_drives_is_foreground_window(self):
        from Xlib import X
        from ok.util import window

        from ok.compat import x11
        if not x11.is_viewable(self.wid):
            self.skipTest('the window never became viewable')
        self.window.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.display.sync()
        if not self._wait_for(lambda: window.is_foreground_window(self.wid)):
            # A Wayland compositor is entitled to refuse focus to an Xwayland client that
            # did not ask through the WM. The predicate under test is still exercised --
            # it correctly reported "not focused" throughout.
            self.skipTest('the compositor did not grant focus to the test window')

        self.assertTrue(window.is_foreground_window(self.wid))

    def test_a_destroyed_window_stops_existing(self):
        from ok.compat import x11
        from ok.util import window

        self.window.destroy()
        self.display.sync()
        self._wait_for(lambda: not x11.exists(self.wid))

        self.assertFalse(x11.exists(self.wid))
        self.assertEqual((0, 0, 0, 0, 0, 0, 1), window.get_window_bounds(self.wid))

    def test_an_override_redirect_toplevel_is_still_enumerated(self):
        """The WM's lists hold only what the WM manages.

        `_NET_CLIENT_LIST` contains managed clients and `WM_STATE` is a property the WM
        sets, so an override-redirect toplevel -- how a client takes the screen without
        asking, a shape fullscreen-exclusive Wine can produce -- is in neither. Trying the
        three sources in order and returning on the first non-empty one made every
        fallback dead code under any EWMH window manager; they are unioned now.
        """
        from Xlib import X, Xatom
        from ok.compat import x11

        screen = self.display.screen()
        unmanaged = screen.root.create_window(
            10, 10, 200, 150, 0, screen.root_depth, X.InputOutput, X.CopyFromParent,
            background_pixel=screen.black_pixel, override_redirect=1)
        unmanaged.change_property(self.display.get_atom('_NET_WM_PID'), Xatom.CARDINAL, 32, [os.getpid()])
        unmanaged.map()
        self.display.sync()
        try:
            self.assertTrue(self._wait_for(lambda: unmanaged.id in x11.list_clients()),
                            'an override-redirect toplevel is invisible to every WM list')
            if _wm_present():
                managed = x11.get_property(_root_id(), '_NET_CLIENT_LIST') or []
                self.assertNotIn(unmanaged.id, [int(w) for w in managed],
                                 'the WM would have to be managing it for this test to prove anything')
        finally:
            unmanaged.destroy()
            self.display.sync()

    def test_monitors_are_reported_as_left_top_right_bottom(self):
        from ok.compat import x11

        monitors = x11.get_monitors()

        self.assertTrue(monitors, 'RandR reported no monitors')
        for left, top, right, bottom in monitors:
            self.assertLess(left, right)
            self.assertLess(top, bottom)
        self.assertIsNotNone(x11.monitor_for(0, 0, 320, 240))

    def test_iconifying_is_visible_to_is_minimized(self):
        from Xlib import X
        from Xlib.protocol import event
        from ok.compat import x11

        if not _wm_present():
            self.skipTest('iconify is a request to a window manager; none is running')

        self.assertFalse(x11.is_minimized(self.wid))
        message = event.ClientMessage(window=self.window,
                                      client_type=self.display.get_atom('WM_CHANGE_STATE'),
                                      data=(32, [x11.ICONIC_STATE, 0, 0, 0, 0]))
        self.display.screen().root.send_event(
            message, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        self.display.sync()
        self._wait_for(lambda: x11.is_minimized(self.wid))

        self.assertTrue(x11.is_minimized(self.wid))
        self.assertIsNotNone(x11.get_abs_geometry(self.wid),
                             'an iconic window keeps its geometry, which is why check_pos cannot see this')

        self.window.map()
        self.display.sync()
        self._wait_for(lambda: not x11.is_minimized(self.wid))
        self.assertFalse(x11.is_minimized(self.wid))

    def test_activate_deiconifies_like_show_window_restore(self):
        """Upstream's `bring_to_front` restores a minimized window before raising it."""
        from Xlib import X
        from Xlib.protocol import event
        from ok.compat import x11

        if not _wm_present():
            self.skipTest('iconify is a request to a window manager; none is running')

        message = event.ClientMessage(window=self.window,
                                      client_type=self.display.get_atom('WM_CHANGE_STATE'),
                                      data=(32, [x11.ICONIC_STATE, 0, 0, 0, 0]))
        self.display.screen().root.send_event(
            message, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        self.display.sync()
        if not self._wait_for(lambda: x11.is_minimized(self.wid)):
            self.skipTest('the window manager did not iconify the test window')

        # The return value is now the *focus* answer, which a focus-stealing-prevention
        # WM is entitled to refuse; the de-iconify half happens either way, and that is
        # what stands in for ShowWindow(SW_RESTORE).
        activated = x11.activate(self.wid)
        self._wait_for(lambda: not x11.is_minimized(self.wid))

        self.assertFalse(x11.is_minimized(self.wid))
        if activated:
            self.assertTrue(x11.is_active(self.wid))

    def test_resize_window_reaches_the_requested_size(self):
        """`width`/`height` are the WINDOW rect, decorations included -- as SetWindowPos.

        Against an undecorated window this is indistinguishable from sizing the client,
        which is why the decorated assertion below exists as well: sizing the client to
        these numbers is the bug this test could not see.
        """
        from ok.compat import x11
        from ok.util import window

        if not _wm_present():
            self.skipTest('resize is negotiated with a window manager; none is running')

        self.assertTrue(window.resize_window(self.wid, 480, 300))

        left, right, top, bottom = x11.get_frame_extents(self.wid)
        _, _, window_width, window_height, width, height = window.get_window_bounds(self.wid)[:6]
        self.assertEqual((480, 300), (window_width, window_height))
        self.assertEqual((480 - left - right, 300 - top - bottom), (width, height))

    def test_resize_window_centres_the_window_rect_not_the_client(self):
        """The frame lands on the monitor centre, computed from the outer dimensions.

        A reparenting WM applies ICCCM win_gravity to a ConfigureRequest, so the
        coordinates handed to `x11.resize` position the *frame*: the client then sits
        `top`/`left` inside it, and the window rect is what ends up centred. Skipped
        unless the WM actually decorates, since undecorated makes the two identical.
        """
        from ok.compat import x11
        from ok.util import window

        if not _wm_present():
            self.skipTest('resize is negotiated with a window manager; none is running')
        self.assertTrue(window.resize_window(self.wid, 480, 300))
        left, right, top, bottom = x11.get_frame_extents(self.wid)
        if not any((left, right, top, bottom)):
            self.skipTest('this window manager draws no decorations; centring is trivially identical')

        x, y = window.get_window_bounds(self.wid)[:2]
        monitor = x11.monitor_for(x, y, 480, 300)
        self.assertIsNotNone(monitor)
        expected_x = monitor[0] + (monitor[2] - monitor[0] - 480) // 2
        expected_y = monitor[1] + (monitor[3] - monitor[1] - 300) // 2

        # The client origin is the frame origin plus the extents.
        self.assertAlmostEqual(expected_x + left, x, delta=2)
        self.assertAlmostEqual(expected_y + top, y, delta=2)

    def test_activate_reports_a_refusal_rather_than_assuming_success(self):
        """Every request `activate` issues is replyless, so the answer has to be read back.

        `MapWindow`, the `_NET_ACTIVE_WINDOW` client message and `ConfigureWindow` all
        return nothing, and their errors are delivered asynchronously to `_on_async_error`
        rather than to `_call` -- so the old body's `return True` after `sync()` reported
        success for a window id that has never existed.
        """
        from ok.compat import x11

        bogus = 0x7fffffff
        self.assertFalse(x11.exists(bogus))

        self.assertFalse(x11.activate(bogus, timeout=0.2))

    def test_resizing_a_window_that_does_not_exist_fails_fast(self):
        """`ConfigureWindow` is replyless too, and `resize` used to return True for any id.

        `resize_window`'s settle loop was not fooled by it -- it polls the real geometry --
        but it paid the full 5 seconds to find out. Asking for the (reply-bearing)
        attributes first makes a dead window a synchronous BadWindow instead.
        """
        from ok.compat import x11
        from ok.util import window

        bogus = 0x7fffffff
        self.assertFalse(x11.exists(bogus))

        self.assertFalse(x11.resize(bogus, 100, 100))
        self.assertFalse(x11.resize(bogus, 100, 100, 0, 0))

        start = time.time()
        self.assertFalse(window.resize_window(bogus, 500, 300))
        self.assertLess(time.time() - start, 2, 'resize_window must not wait out its settle loop')

    def test_resizing_a_live_window_still_succeeds(self):
        """The guard must not turn a real resize into a refusal."""
        from ok.compat import x11

        self.assertTrue(x11.resize(self.wid, 400, 260))


if __name__ == '__main__':
    unittest.main()
