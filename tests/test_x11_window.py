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
import os
import pathlib
import sys
import threading
import time
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
        window = self._get(wid)
        if not window:
            return False
        self.resized.append((wid, width, height, x, y))
        window.geometry = (x if x is not None else window.geometry[0],
                           y if y is not None else window.geometry[1], width, height)
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

    def test_player_id_filters_on_the_command_line(self):
        fake = FakeX11([FakeWindow(0x1400001, pid=4242, name='emulator')])
        candidates = {4242: ([('dnplayer.exe', '/games/dnplayer.exe')], ['/games/dnplayer.exe', '3'])}

        self.assertEqual(0x1400001, self.run_find(fake, candidates, exe_names=['dnplayer.exe'], player_id=3)[1])
        self.assertEqual(0, self.run_find(fake, candidates, exe_names=['dnplayer.exe'], player_id=5)[1])


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

    def test_missing_pactl_is_not_an_error(self):
        from ok.device.capture_methods import x11_window

        with unittest.mock.patch.object(x11_window, 'x11', FakeX11([FakeWindow(0x1400001, pid=4242)])), \
                unittest.mock.patch.object(x11_window.shutil, 'which', return_value=None):
            self.assertEqual(0, x11_window.get_mute_state(0x1400001))
            x11_window.set_mute_state(0x1400001, 1)


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

    @classmethod
    def _init_attributes(cls, node):
        init = next(n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
        return {target.attr
                for statement in ast.walk(init)
                for target in getattr(statement, 'targets', [])
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == 'self'}

    def test_the_constructor_sets_every_attribute_upstream_sets(self):
        upstream = self._class_node('ok/device/capture_methods/hwnd_window.py', 'HwndWindow')
        linux = self._class_node('ok/device/capture_methods/x11_window.py', 'X11Window')

        missing = self._init_attributes(upstream) - self._init_attributes(linux)

        self.assertEqual(set(), missing,
                         'HwndWindow.__init__ gained attributes; mirror them in X11Window.__init__')

    def test_every_upstream_method_is_inherited_or_overridden(self):
        from ok.device.capture_methods.hwnd_window import HwndWindow
        from ok.device.capture_methods.x11_window import X11Window

        self.assertTrue(issubclass(X11Window, HwndWindow))
        missing = [name for name in vars(HwndWindow) if not name.startswith('__')
                   and not hasattr(X11Window, name)]
        self.assertEqual([], missing)

    def test_the_linux_modules_call_no_win32(self):
        for path in ('ok/compat/x11.py', 'ok/compat/window_x11.py',
                     'ok/device/capture_methods/x11_window.py'):
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

    def test_resize_window_reaches_the_requested_size(self):
        from ok.util import window

        if not _wm_present():
            self.skipTest('resize is negotiated with a window manager; none is running')

        self.assertTrue(window.resize_window(self.wid, 480, 300))

        _, _, _, _, width, height = window.get_window_bounds(self.wid)[:6]
        self.assertEqual((480, 300), (width, height))


if __name__ == '__main__':
    unittest.main()
