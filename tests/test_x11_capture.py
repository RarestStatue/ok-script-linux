"""Phase 3 of the Linux port: the X11 capture backend.

Three kinds of test, as in ``test_x11_window.py``:

* **Pixel-format tests** over ``xshm.image_to_bgr``, built on hand-made ``XImage`` structs.
  They are the cheap half of the one bug class that is invisible at runtime: a frame whose
  channels are swapped is a *picture*, not an error, and every template match downstream
  quietly gets worse.
* **Contract tests** over ``X11CaptureMethod``: the crop rectangle it asks for, the
  ``clickable()`` override [V15], the minimized-window exception, and the fall-through in
  ``update_capture_method`` when the pixel path is unavailable.
* **Live tests** against a real X server: known colours in, known colours out, through both
  the shared-memory and the wire path, plus the copy contract the SHM path depends on.
  They skip without a display.
"""

import ctypes
import os
import sys
import time
import unittest
import unittest.mock

import numpy as np

skip_on_windows = unittest.skipIf(sys.platform == 'win32',
                                  'the Linux capture backend is inert on Windows')

BGRA = (0xff0000, 0xff00, 0xff)   # red, green, blue masks of a depth-24 TrueColor visual


DEPTH30 = (0x3FF00000, 0x000FFC00, 0x000003FF)   # red, green, blue of a 10-bit visual


def make_image(pixels, byte_order=0, masks=BGRA, stride=None, bits_per_pixel=32, depth=24):
    """A real ``XImage`` over a Python buffer. Returns ``(pointer, buffer)``.

    The buffer must outlive the pointer, which is why it comes back too.
    """
    from ok.compat import xshm

    height, width = pixels.shape[0], pixels.shape[1]
    stride = stride or width * 4
    buffer = (ctypes.c_ubyte * (stride * height))()
    for row in range(height):
        start = row * stride
        buffer[start:start + width * 4] = bytes(pixels[row].reshape(-1).tolist())
    image = xshm.XImage(width=width, height=height, format=xshm.Z_PIXMAP,
                        data=ctypes.cast(buffer, ctypes.c_void_p).value,
                        byte_order=byte_order, depth=depth, bytes_per_line=stride,
                        bits_per_pixel=bits_per_pixel,
                        red_mask=masks[0], green_mask=masks[1], blue_mask=masks[2])
    return ctypes.pointer(image), buffer


def make_10bit_image(r, g, b, byte_order=0):
    """One depth-30 pixel. ``bits_per_pixel`` is still 32, which is the whole trap."""
    word = (r << 20) | (g << 10) | b
    order = range(0, 32, 8) if byte_order == 0 else range(24, -8, -8)
    pixels = np.array([[[(word >> s) & 0xff for s in order]]], dtype=np.uint8)
    return make_image(pixels, byte_order=byte_order, masks=DEPTH30, depth=30)


@skip_on_windows
class TestImageToBgr(unittest.TestCase):
    """The pixel unpacking: stride, channel order, and the pixmap mask fallback."""

    def test_bgra_little_endian_is_the_measured_layout(self):
        from ok.compat import xshm

        # one pixel: B=1 G=2 R=3, alpha ignored
        pixels = np.array([[[1, 2, 3, 255]]], dtype=np.uint8)
        image, _buffer = make_image(pixels)

        frame = xshm.image_to_bgr(image)

        self.assertEqual((1, 1, 3), frame.shape)
        self.assertEqual([1, 2, 3], frame[0, 0].tolist())
        self.assertTrue(frame.flags['C_CONTIGUOUS'])

    def test_the_stride_is_honoured_not_width_times_four(self):
        """`bytes_per_line` is 7680 for a 1920px image here; padded rows must not shear."""
        from ok.compat import xshm

        pixels = np.zeros((2, 3, 4), dtype=np.uint8)
        pixels[0, 0] = (10, 20, 30, 0)
        pixels[1, 2] = (40, 50, 60, 0)
        image, _buffer = make_image(pixels, stride=3 * 4 + 8)

        frame = xshm.image_to_bgr(image)

        self.assertEqual((2, 3, 3), frame.shape)
        self.assertEqual([10, 20, 30], frame[0, 0].tolist())
        self.assertEqual([40, 50, 60], frame[1, 2].tolist())
        self.assertTrue(frame.flags['C_CONTIGUOUS'])

    def test_a_pixmap_grab_gets_its_masks_from_the_windows_visual(self):
        """`XGetImage`/`XShmGetImage` zero the masks when the drawable is a Pixmap.

        Both fill the image's masks in from the *reply's* visual id, and a pixmap has no
        visual -- so the composite path's frames arrive with `red_mask == 0` and the
        generic unpacking below has nothing to go on. The window's own visual is the
        fallback. Found by running the composite path against a real server, where it
        raised rather than mis-coloured; a mis-colour is the failure mode if the fallback
        is ever silently dropped.
        """
        from ok.compat import xshm

        pixels = np.array([[[7, 8, 9, 0]]], dtype=np.uint8)
        image, _buffer = make_image(pixels, masks=(0, 0, 0))

        with self.assertRaises(ValueError):
            xshm.image_to_bgr(image)

        frame = xshm.image_to_bgr(image, masks=BGRA)
        self.assertEqual([7, 8, 9], frame[0, 0].tolist())

    def test_a_big_endian_server_is_unpacked_by_its_masks(self):
        from ok.compat import xshm

        # MSBFirst: the same 0x00RRGGBB word lands as [alpha, R, G, B] in memory.
        pixels = np.array([[[0, 3, 2, 1]]], dtype=np.uint8)
        image, _buffer = make_image(pixels, byte_order=1)

        frame = xshm.image_to_bgr(image)

        self.assertEqual([1, 2, 3], frame[0, 0].tolist())

    def test_an_unsupported_depth_says_so(self):
        from ok.compat import xshm

        pixels = np.zeros((1, 1, 4), dtype=np.uint8)
        image, _buffer = make_image(pixels, bits_per_pixel=16)

        with self.assertRaises(ValueError) as caught:
            xshm.image_to_bgr(image)
        self.assertIn('16 bits per pixel', str(caught.exception))

    def test_a_ten_bit_visual_is_not_read_as_eight_bit(self):
        """A depth-30 visual lands on the BGRA fast path unless the mask *width* is checked.

        Unfixed, mid grey came back as [0, 2, 8] -- not a tint, a picture whose luminance is
        not even monotonic, with no exception and no log line.
        """
        from ok.compat import xshm

        for (r, g, b), expected in (((1023, 0, 0), [0, 0, 255]),
                                    ((0, 1023, 0), [0, 255, 0]),
                                    ((0, 0, 1023), [255, 0, 0]),
                                    ((512, 512, 512), [128, 128, 128]),
                                    ((1023, 1023, 1023), [255, 255, 255]),
                                    ((0, 0, 0), [0, 0, 0])):
            with self.subTest(pixel=(r, g, b)):
                image, _buffer = make_10bit_image(r, g, b)
                self.assertEqual(expected, xshm.image_to_bgr(image)[0, 0].tolist())

    def test_a_ten_bit_visual_on_a_big_endian_server(self):
        from ok.compat import xshm

        image, _buffer = make_10bit_image(1023, 0, 0, byte_order=1)
        self.assertEqual([0, 0, 255], xshm.image_to_bgr(image)[0, 0].tolist())

    def test_the_eight_bit_path_still_takes_the_cheap_copy(self):
        """The regression guard for the fix: depth 24 must not fall into `_unpack_wide`."""
        from ok.compat import xshm

        pixels = np.array([[[1, 2, 3, 255]]], dtype=np.uint8)
        image, _buffer = make_image(pixels)
        self.assertEqual((0, 1, 2), xshm._channel_indices(image.contents))
        self.assertEqual([1, 2, 3], xshm.image_to_bgr(image)[0, 0].tolist())


class FakeHwndWindow:
    """Everything `X11CaptureMethod` reads off the window object."""

    def __init__(self, hwnd=0x2a, width=1920, height=1080, client_width=None, client_height=None,
                 real_x_offset=0, real_y_offset=0, visible=False, exists=True):
        self.hwnd = hwnd
        self.width = width
        self.height = height
        self.client_width = width if client_width is None else client_width
        self.client_height = height if client_height is None else client_height
        self.real_x_offset = real_x_offset
        self.real_y_offset = real_y_offset
        self.real_width = self.client_width
        self.real_height = self.client_height
        self.visible = visible
        self.exists = exists
        self.x = 100
        self.y = 200


class FakeGrabber:
    """Stands in for `xshm.X11Grabber`: records what was asked for, returns what it is told."""

    def __init__(self, use_composite=False, frame=None, geometry=(1920, 1080, 0, 24, 2)):
        self.use_composite = use_composite
        self.frame = frame
        self.geometry = geometry
        self.calls = []
        self.closed = False
        self._composite_failed = False

    @property
    def composite_active(self):
        return bool(self.use_composite and not self._composite_failed)

    def window_geometry(self, wid):
        return self.geometry

    def grab(self, wid, x, y, width, height):
        self.calls.append((wid, x, y, width, height))
        return self.frame

    def close(self):
        self.closed = True


@skip_on_windows
class TestCaptureRect(unittest.TestCase):
    """The rectangle to grab. Wrong here means a frame that is offset or letterboxed."""

    def rect(self, **kwargs):
        from ok.device.capture_methods.x11_capture import capture_rect
        return capture_rect(FakeHwndWindow(**kwargs), 640, 480)

    def test_an_unletterboxed_window_is_grabbed_whole(self):
        self.assertEqual((0, 0, 1920, 1080), self.rect())

    def test_a_letterboxed_window_is_cropped_like_get_capture_origin(self):
        """`HwndWindow.get_capture_origin` is the sibling of this and must agree.

        `do_update_window_size` shrinks `height` to the frame's aspect ratio while
        `client_height` keeps the window's own size, and the overlay is positioned from
        `get_capture_origin`. A capture that cropped from a different origin would place
        every click correctly and match every template against the wrong pixels.
        """
        from ok.device.capture_methods.hwnd_window import HwndWindow

        window = FakeHwndWindow(width=1600, height=900, client_width=1620, client_height=960)
        from ok.device.capture_methods.x11_capture import capture_rect
        x, y, width, height = capture_rect(window)

        origin_x, origin_y = HwndWindow.get_capture_origin(window)
        self.assertEqual((origin_x - window.x, origin_y - window.y), (x, y))
        self.assertEqual((1600, 900), (width, height))

    def test_the_real_offsets_branch_is_upstreams_and_still_works(self):
        self.assertEqual((12, 34, 1920, 1080), self.rect(real_x_offset=12, real_y_offset=34))

    def test_before_the_first_poll_the_live_window_size_is_used(self):
        """`get_frame` can be called before `do_update_window_size` has filled anything in."""
        self.assertEqual((0, 0, 640, 480), self.rect(width=0, height=0))


@skip_on_windows
class TestX11CaptureMethod(unittest.TestCase):

    def build(self, grabber=None, **kwargs):
        from ok.device.capture_methods.x11_capture import X11CaptureMethod

        window = FakeHwndWindow(**kwargs)
        method = X11CaptureMethod(window)
        method.grabber = grabber if grabber is not None else FakeGrabber()
        method.exit_event = unittest.mock.Mock(is_set=lambda: False)
        return method

    def test_clickable_is_true_even_while_the_window_is_backgrounded(self):
        """The base returns `hwnd_window.visible`, which is a FOREGROUND test [V15].

        False for the whole of background play, which is what the port exists for.
        """
        method = self.build(visible=False)

        self.assertTrue(method.clickable())

    def test_a_frame_is_grabbed_at_the_cropped_rectangle(self):
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        grabber = FakeGrabber(frame=frame)
        method = self.build(grabber, width=1600, height=900, client_width=1620, client_height=960)

        self.assertIs(frame, method.get_frame())
        self.assertEqual([(0x2a, 10, 50, 1600, 900)], grabber.calls)

    def test_the_base_class_does_not_have_to_drop_an_alpha_channel(self):
        """`do_get_frame` returns 3 channels, so `base.get_frame`'s slice never runs."""
        grabber = FakeGrabber(frame=np.zeros((900, 1600, 3), dtype=np.uint8))
        method = self.build(grabber)

        frame = method.get_frame()

        self.assertEqual(3, frame.shape[2])
        self.assertEqual((1600, 900), (method.width, method.height))

    def test_no_hwnd_is_no_frame_and_no_grab(self):
        grabber = FakeGrabber()
        method = self.build(grabber, hwnd=0)

        self.assertIsNone(method.get_frame())
        self.assertEqual([], grabber.calls)

    def test_a_minimized_window_is_no_frame_and_does_not_kill_the_task(self):
        """A CaptureException here reaches TaskExecutor.py:639 -> task.disable().

        Minimizing the game must pause the bot (the window layer does that through
        `pos_valid`), never switch the task off. Returning None is what keeps that true.
        """
        from ok.device.capture_methods import x11_capture

        method = self.build(FakeGrabber(frame=None))
        with unittest.mock.patch.object(x11_capture.x11, 'exists', return_value=True), \
                unittest.mock.patch.object(x11_capture.x11, 'is_minimized', return_value=True):
            self.assertIsNone(method.get_frame())
            self.assertIsNone(method.get_frame())      # reported once, not per poll

    def test_the_minimized_notice_is_logged_once_per_episode(self):
        from ok.device.capture_methods import x11_capture

        grabber = FakeGrabber(frame=None)
        method = self.build(grabber)
        minimized = lambda: (
            unittest.mock.patch.object(x11_capture.x11, 'exists', return_value=True),
            unittest.mock.patch.object(x11_capture.x11, 'is_minimized', return_value=True))

        exists, is_minimized = minimized()
        with exists, is_minimized, \
                unittest.mock.patch.object(x11_capture.logger, 'info') as info:
            method.get_frame()
            method.get_frame()
            self.assertEqual(1, info.call_count)

        grabber.frame = np.zeros((900, 1600, 3), dtype=np.uint8)   # window restored
        method.get_frame()

        exists, is_minimized = minimized()
        grabber.frame = None
        with exists, is_minimized, \
                unittest.mock.patch.object(x11_capture.logger, 'info') as info:
            method.get_frame()
            self.assertEqual(1, info.call_count)       # a new episode reports again

    def test_a_window_that_no_longer_exists_is_not_reported_as_minimized(self):
        """`is_minimized`'s last resort is "not viewable", which a dead id answers True.

        Unguarded, a game that exited raised "the game window is minimized" on every poll
        until the window layer noticed -- the one message a user cannot act on.
        """
        from ok.device.capture_methods import x11_capture

        method = self.build(FakeGrabber(frame=None))
        with unittest.mock.patch.object(x11_capture.x11, 'exists', return_value=False), \
                unittest.mock.patch.object(x11_capture.x11, 'is_minimized', return_value=True):
            self.assertIsNone(method.get_frame())

    def test_a_failed_grab_on_a_live_window_is_just_no_frame(self):
        from ok.device.capture_methods import x11_capture

        method = self.build(FakeGrabber(frame=None))
        with unittest.mock.patch.object(x11_capture.x11, 'exists', return_value=True), \
                unittest.mock.patch.object(x11_capture.x11, 'is_minimized', return_value=False):
            self.assertIsNone(method.get_frame())

    def test_a_window_with_no_geometry_yet_and_none_on_the_server_is_no_frame(self):
        grabber = FakeGrabber(geometry=None)
        method = self.build(grabber, width=0, height=0)

        self.assertIsNone(method.get_frame())
        self.assertEqual([], grabber.calls)

    def test_the_servers_size_is_only_asked_for_before_the_first_poll(self):
        """`grab` reads the window's attributes anyway; a second round trip buys nothing."""
        grabber = FakeGrabber(frame=np.zeros((1080, 1920, 3), dtype=np.uint8))
        grabber.window_geometry = lambda wid: self.fail('window_geometry should not be called')
        method = self.build(grabber)

        self.assertIsNotNone(method.get_frame())

    def test_connected_asks_the_server_not_the_cached_flag(self):
        from ok.device.capture_methods import x11_capture

        method = self.build()
        with unittest.mock.patch.object(x11_capture.x11, 'exists', return_value=False):
            self.assertFalse(method.connected())
        with unittest.mock.patch.object(x11_capture.x11, 'exists', return_value=True):
            self.assertTrue(method.connected())

    def test_switching_to_composite_rebuilds_the_grabber(self):
        """`get_capture` hands back the same object when the method name changes."""
        from ok.device.capture_methods import x11_capture

        grabber = FakeGrabber(frame=np.zeros((1080, 1920, 3), dtype=np.uint8))
        method = self.build(grabber)
        self.assertEqual('X11', method.get_name())

        with unittest.mock.patch.object(x11_capture, 'use_composite', True):
            method.get_frame()
            self.assertTrue(grabber.closed)
            self.assertIsNot(grabber, method.grabber)
            self.assertTrue(method.grabber.use_composite)
            self.assertEqual('X11_Composite', method.get_name())

    def test_the_name_follows_the_path_actually_taken(self):
        """`use_composite` is what was asked for; the fallback is silent and permanent."""
        method = self.build(FakeGrabber(use_composite=True))

        self.assertEqual('X11_Composite', method.get_name())

        method.grabber._composite_failed = True          # silently fell back
        self.assertEqual('X11', method.get_name())

    def test_close_releases_the_display_and_the_segment(self):
        grabber = FakeGrabber()
        method = self.build(grabber)

        method.close()

        self.assertTrue(grabber.closed)


@skip_on_windows
class TestUpdateCaptureMethod(unittest.TestCase):
    """Selection: the two names are one class and one flag, as BitBlt_RenderFull is."""

    def select(self, methods, available=True):
        import ok.device.capture_methods.update as update

        with unittest.mock.patch.object(update, 'x11_capture_available', return_value=available):
            return update.update_capture_method({'capture_method': methods}, None,
                                                FakeHwndWindow(), exit_event=None)

    def test_x11_is_selected_and_named(self):
        from ok.device.capture_methods.x11_capture import X11CaptureMethod

        capture = self.select(['X11'])

        self.assertIsInstance(capture, X11CaptureMethod)
        self.assertEqual('X11', capture.get_name())
        self.assertFalse(capture.grabber.use_composite)
        capture.close()

    def test_x11_composite_sets_the_flag(self):
        capture = self.select(['X11_Composite'])

        self.assertEqual('X11_Composite', capture.get_name())
        self.assertTrue(capture.grabber.use_composite)
        capture.close()

    def test_switching_the_path_takes_effect_before_the_next_frame(self):
        """`get_capture` reuses the object, so `update_capture_method` must not wait.

        Left to `do_get_frame`'s lazy rebuild, `get_name()` reports the old path until a
        frame happens to be asked for -- and that name is what `DeviceManager`, the GUI and
        `tools/check_linux_startup.py` log.
        """
        import ok.device.capture_methods.update as update

        capture = self.select(['X11'])
        self.assertEqual('X11', capture.get_name())

        with unittest.mock.patch.object(update, 'x11_capture_available', return_value=True):
            same = update.update_capture_method({'capture_method': ['X11_Composite']}, capture,
                                                capture.hwnd_window, exit_event=None)

        self.assertIs(capture, same)
        self.assertTrue(same.grabber.use_composite)
        self.assertEqual('X11_Composite', same.get_name())   # without a frame first
        same.close()

    def test_an_unanswerable_display_is_not_available(self):
        """A stale DISPLAY passes `xshm.available()` and then fails every grab.

        `xshm.available()` is `the libraries load` and `DISPLAY is set`; neither proves a
        server answers. Measured before the fix with `DISPLAY=:99`:
        `x11_capture_available()` True, `x11.available()` False, and one ERROR per grab.

        `x11.available` is patched rather than pointing the real DISPLAY at a dead server:
        `ok/compat/x11.py` memoises its connection in a module global, so a test that
        actually connects to `:99` leaves a None behind and every live test later in the
        same process silently skips or fails.
        """
        from ok.compat import x11
        from ok.device.capture_methods import x11_capture

        with unittest.mock.patch.object(x11, 'available', return_value=False), \
                unittest.mock.patch('ok.compat.xshm.available', return_value=True):
            self.assertFalse(x11_capture.x11_capture_available())
        with unittest.mock.patch.object(x11, 'available', return_value=True), \
                unittest.mock.patch('ok.compat.xshm.available', return_value=True):
            self.assertTrue(x11_capture.x11_capture_available())

    def test_an_unavailable_pixel_path_falls_through_to_the_next_method(self):
        """No libX11, no DISPLAY: the next entry in the user's list must get its turn.

        Returning a capture object that can never produce a frame would strand the app on
        it, which is the shape `get_win_graphics_capture` avoids for WGC.
        """
        import ok.device.capture_methods.update as update

        with unittest.mock.patch.object(update, 'x11_capture_available', return_value=False), \
                unittest.mock.patch.object(update, 'get_capture') as get_capture:
            self.assertIsNone(update.update_capture_method({'capture_method': ['X11']}, None,
                                                           FakeHwndWindow(), exit_event=None))
        get_capture.assert_not_called()

    def tearDown(self):
        from ok.device.capture_methods import x11_capture
        x11_capture.use_composite = False


@skip_on_windows
class TestLiveCapture(unittest.TestCase):
    """A real window on a real server: known colours in, known colours out."""

    WIDTH, HEIGHT = 320, 240

    @classmethod
    def setUpClass(cls):
        from ok.compat import xshm
        if not xshm.available():
            raise unittest.SkipTest('no usable X11 display for the pixel path')

    def setUp(self):
        from Xlib import X, Xatom, display
        from ok.compat import x11

        self.display = display.Display()
        screen = self.display.screen()
        self.window = screen.root.create_window(
            80, 60, self.WIDTH, self.HEIGHT, 0, screen.root_depth, X.InputOutput,
            X.CopyFromParent, background_pixel=screen.white_pixel,
            event_mask=X.StructureNotifyMask)
        self.window.set_wm_name('ok-script x11 capture test window')
        self.window.change_property(self.display.get_atom('_NET_WM_PID'), Xatom.CARDINAL, 32,
                                    [os.getpid()])
        self.window.map()
        self.display.sync()
        self.wid = self.window.id
        if not self._wait_for(lambda: x11.is_viewable(self.wid)):
            self.skipTest('the test window never became viewable')
        self._paint()
        self.grabbers = []

    def tearDown(self):
        for grabber in getattr(self, 'grabbers', []):
            grabber.close()
        try:
            self.window.destroy()
            self.display.sync()
            self.display.close()
        except Exception:
            pass

    def _paint(self):
        """Left half blue, right half red, a green square at the origin. X11 pixels are RGB."""
        blue = self.window.create_gc(foreground=0x0000FF)
        red = self.window.create_gc(foreground=0xFF0000)
        green = self.window.create_gc(foreground=0x00FF00)
        self.window.fill_rectangle(blue, 0, 0, self.WIDTH // 2, self.HEIGHT)
        self.window.fill_rectangle(red, self.WIDTH // 2, 0, self.WIDTH // 2, self.HEIGHT)
        self.window.fill_rectangle(green, 0, 0, 8, 8)
        self.display.sync()
        time.sleep(0.2)

    @staticmethod
    def _wait_for(predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def _grabber(self, **kwargs):
        from ok.compat import xshm
        grabber = xshm.X11Grabber(**kwargs)
        self.grabbers.append(grabber)
        return grabber

    def _assert_painted(self, frame):
        self.assertEqual((self.HEIGHT, self.WIDTH, 3), frame.shape)
        self.assertEqual([0, 255, 0], frame[4, 4].tolist(), 'the green square is not BGR')
        self.assertEqual([255, 0, 0], frame[self.HEIGHT // 2, 20].tolist(), 'left half is not blue')
        self.assertEqual([0, 0, 255], frame[self.HEIGHT // 2, self.WIDTH - 20].tolist(),
                         'right half is not red')

    def test_the_shared_memory_path_returns_bgr(self):
        grabber = self._grabber()

        frame = grabber.grab(self.wid, 0, 0, self.WIDTH, self.HEIGHT)

        self._assert_painted(frame)
        self.assertTrue(frame.flags['C_CONTIGUOUS'])
        if not grabber.shm_active:
            self.skipTest('MIT-SHM is unavailable on this display; the wire path was tested instead')

    def test_the_wire_path_returns_the_same_pixels(self):
        """`XGetImage` is the fallback when the server shares no memory with us [V14]."""
        shared = self._grabber().grab(self.wid, 0, 0, self.WIDTH, self.HEIGHT)

        wire = self._grabber()
        wire._shm_checked, wire._shm_usable = True, False
        frame = wire.grab(self.wid, 0, 0, self.WIDTH, self.HEIGHT)

        self._assert_painted(frame)
        self.assertFalse(wire.shm_active)
        np.testing.assert_array_equal(shared, frame)

    def test_a_frame_survives_the_next_grab(self):
        """The SHM segment is reused; a view into it would corrupt frames in flight [V14]."""
        grabber = self._grabber()

        first = grabber.grab(self.wid, 0, 0, self.WIDTH, self.HEIGHT)
        before = first.copy()
        grabber.grab(self.wid, 0, 0, self.WIDTH, self.HEIGHT)

        np.testing.assert_array_equal(before, first)

    def test_a_cropped_grab_takes_the_requested_rectangle(self):
        grabber = self._grabber()

        frame = grabber.grab(self.wid, self.WIDTH // 2, 0, self.WIDTH // 2, self.HEIGHT)

        self.assertEqual((self.HEIGHT, self.WIDTH // 2, 3), frame.shape)
        self.assertEqual([0, 0, 255], frame[10, 10].tolist())

    def test_a_stale_rectangle_is_clamped_to_the_window(self):
        """The caller's numbers are up to 0.2 s old; XGetImage answers BadMatch, not a crop."""
        grabber = self._grabber()

        frame = grabber.grab(self.wid, 0, 0, self.WIDTH * 4, self.HEIGHT * 4)

        self.assertEqual((self.HEIGHT, self.WIDTH, 3), frame.shape)

    def test_a_resize_reallocates_the_segment(self):
        grabber = self._grabber()
        self.assertEqual((self.HEIGHT, self.WIDTH, 3),
                         grabber.grab(self.wid, 0, 0, self.WIDTH, self.HEIGHT).shape)

        self.window.configure(width=200, height=150)
        self.display.sync()
        if not self._wait_for(lambda: grabber.window_geometry(self.wid)[:2] == (200, 150)):
            self.skipTest('the server did not apply the resize')

        frame = grabber.grab(self.wid, 0, 0, 200, 150)
        self.assertEqual((150, 200, 3), frame.shape)

    def test_a_destroyed_window_is_none_rather_than_a_dead_process(self):
        """Xlib's default error handler calls exit(1); this test fails by *not returning*."""
        from ok.compat import x11

        grabber = self._grabber()
        grabber.grab(self.wid, 0, 0, self.WIDTH, self.HEIGHT)
        self.window.destroy()
        self.display.sync()
        self._wait_for(lambda: not x11.exists(self.wid))

        self.assertIsNone(grabber.grab(self.wid, 0, 0, self.WIDTH, self.HEIGHT))
        self.assertIsNone(grabber.window_geometry(self.wid))

    def test_the_composite_path_returns_the_same_pixels(self):
        """XComposite is for plain X11, where an occluded window is genuinely not in the
        framebuffer. On Xwayland the direct path already handles occlusion [V7]."""
        from ok.compat import xshm

        if not xshm.composite_available():
            self.skipTest('libXcomposite is not installed')

        direct = self._grabber().grab(self.wid, 0, 0, self.WIDTH, self.HEIGHT)
        grabber = self._grabber(use_composite=True)
        frame = grabber.grab(self.wid, 0, 0, self.WIDTH, self.HEIGHT)
        if frame is None:
            self.skipTest('the server refused the composite redirect')

        self._assert_painted(frame)
        np.testing.assert_array_equal(direct, frame)

    def test_an_iconified_window_is_no_frame_not_a_stale_one(self):
        """Iconic means unmapped, and an unmapped window has no pixels [V7].

        The failure this guards is not an exception -- it is the opposite: a compositing WM
        keeps a backing pixmap alive, so a capture that did not check `map_state` could go
        on returning the last frame the game drew, forever, and every task downstream would
        act on a picture of the past.

        It must be None and *not* a `CaptureException`: one out of a task reaches
        `TaskExecutor.py:639` and is answered with `task.disable()`, which would turn a
        minimize into a switched-off task. The window layer pauses instead, reversibly.
        """
        from Xlib import X
        from Xlib.protocol import event
        from ok.compat import x11
        from ok.device.capture_methods.x11_capture import X11CaptureMethod

        if not x11.get_property(self.display.screen().root.id, '_NET_SUPPORTING_WM_CHECK'):
            self.skipTest('iconify is a request to a window manager; none is running')

        method = X11CaptureMethod(FakeHwndWindow(hwnd=self.wid, width=self.WIDTH,
                                                 height=self.HEIGHT))
        method.exit_event = unittest.mock.Mock(is_set=lambda: False)
        self.grabbers.append(method.grabber)
        try:
            self._assert_painted(method.get_frame())

            message = event.ClientMessage(window=self.window,
                                          client_type=self.display.get_atom('WM_CHANGE_STATE'),
                                          data=(32, [x11.ICONIC_STATE, 0, 0, 0, 0]))
            self.display.screen().root.send_event(
                message, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
            self.display.sync()
            if not self._wait_for(lambda: x11.is_minimized(self.wid)):
                self.skipTest('the window manager did not iconify the test window')

            self.assertIsNone(method.get_frame())

            self.window.map()
            self.display.sync()
            self._wait_for(lambda: not x11.is_minimized(self.wid))
            self._paint()
            self._assert_painted(method.get_frame())
        finally:
            method.close()

    def test_the_capture_method_produces_a_frame_for_this_window(self):
        """End to end: the class `update_capture_method` builds, against a real window."""
        from ok.device.capture_methods.x11_capture import X11CaptureMethod

        method = X11CaptureMethod(FakeHwndWindow(hwnd=self.wid, width=self.WIDTH,
                                                 height=self.HEIGHT))
        method.exit_event = unittest.mock.Mock(is_set=lambda: False)
        self.grabbers.append(method.grabber)
        try:
            frame = method.get_frame()
            self._assert_painted(frame)
            self.assertEqual((self.WIDTH, self.HEIGHT), (method.width, method.height))
            self.assertTrue(method.connected())
            self.assertTrue(method.clickable())
        finally:
            method.close()


if __name__ == '__main__':
    unittest.main()
