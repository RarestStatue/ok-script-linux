"""Regression tests for the Linux Win32 compatibility layer (Phase 1 of the Linux port).

The shim is installed by the root `conftest.py` before collection, which is the same
ordering requirement real Linux entry points have.
"""

import ast
import importlib
import os
import pathlib
import re
import sys
import unittest
import unittest.mock

REPO = pathlib.Path(__file__).resolve().parent.parent

skip_on_windows = unittest.skipIf(sys.platform == 'win32',
                                  'the Linux compatibility layer is inert on Windows')


@skip_on_windows
class TestWin32ConConstants(unittest.TestCase):
    """`win32con` must carry real integers, not stubs.

    This is the silent-corruption hazard of the port: with `win32con` stubbed,
    `keys.py` still imports and `vk_key_dict['F1']` becomes a stub object rather than
    0x70, so the input backend posts garbage virtual-key codes with nothing raising.
    """

    def test_vk_key_dict_values_are_ints(self):
        from ok.device.interaction_methods.keys import vk_key_dict

        self.assertTrue(vk_key_dict, 'vk_key_dict is empty')
        non_int = {k: v for k, v in vk_key_dict.items() if not isinstance(v, int)}
        self.assertEqual({}, non_int)
        self.assertEqual(0x70, vk_key_dict['F1'])

    def test_spot_check_values_against_the_win32_api(self):
        import win32con

        # Values documented by the Win32 API; a transcription slip in the generated
        # module would show up here rather than as unexplained in-game behaviour.
        self.assertEqual(0x0100, win32con.WM_KEYDOWN)
        self.assertEqual(0x0101, win32con.WM_KEYUP)
        self.assertEqual(0x0200, win32con.WM_MOUSEMOVE)
        self.assertEqual(0x0201, win32con.WM_LBUTTONDOWN)
        self.assertEqual(120, win32con.WHEEL_DELTA)
        self.assertEqual(-16, win32con.GWL_STYLE)
        self.assertEqual(0x1B, win32con.VK_ESCAPE)

    def test_every_constant_the_tree_uses_is_covered(self):
        """The generated subset must keep up with the source tree."""
        import win32con

        used = set()
        pattern = re.compile(r'(?<![\w.])win32con\.(\w+)')
        excluded = {REPO / 'ok' / 'compat' / 'win32con_constants.py',
                    REPO / 'ok' / 'compat' / 'winreg_constants.py',
                    # this file: it scans for the same pattern, and reads an absent
                    # constant on purpose below. Neither is a real usage.
                    pathlib.Path(__file__).resolve()}
        # tests/ too: test_notifications.py uses six constants of its own, and a new one
        # there must fail here rather than at run time. Matches tools/gen_win32con.py.
        for root in ('ok', 'tests'):
            for path in (REPO / root).rglob('*.py'):
                if path in excluded:
                    continue
                used.update(pattern.findall(path.read_text(encoding='utf-8')))

        missing = sorted(n for n in used if not hasattr(win32con, n))
        self.assertEqual([], missing,
                         'regenerate with `python3 tools/gen_win32con.py`')

    def test_unknown_constant_raises_instead_of_stubbing(self):
        import win32con

        with self.assertRaises(AttributeError):
            win32con.WM_NOT_A_REAL_MESSAGE


@skip_on_windows
class TestWin32Stub(unittest.TestCase):
    def test_ctypes_windows_names_exist(self):
        import ctypes

        # `from ctypes import windll` and `from ctypes import *` must both work.
        from ctypes import windll  # noqa: F401
        self.assertIs(ctypes.HRESULT, ctypes.c_long)
        self.assertIs(ctypes.WINFUNCTYPE, ctypes.CFUNCTYPE)

    def test_dll_loaders_return_a_handle_stub_rather_than_raising(self):
        """Four modules call a loader at import time; raising there breaks the tree."""
        import ctypes

        handle = ctypes.WinDLL('user32', use_last_error=True)
        self.assertIsNotNone(handle)
        self.assertIsNotNone(ctypes.windll.LoadLibrary('combase.dll'))
        # Attribute access off a handle keeps chaining...
        self.assertIsNotNone(handle.MonitorFromWindow)

    def test_calling_a_real_win32_function_raises_naming_the_symbol(self):
        """Imports succeed; actually needing Windows fails loudly and locally."""
        import win32gui

        with self.assertRaises(NotImplementedError) as caught:
            win32gui.PostMessage(0, 0, 0, 0)
        self.assertIn('win32gui.PostMessage', str(caught.exception))

    def test_dunder_lookups_still_fail_normally(self):
        """Otherwise copy/pickle/inspect/mock get handed a stub where a method is due."""
        import win32gui

        with self.assertRaises(AttributeError):
            win32gui.__deepcopy__

    def test_install_is_idempotent(self):
        import ctypes

        from ok.compat.win32_stub import install

        before = ctypes.windll
        install()
        self.assertIs(before, ctypes.windll)

    def test_winreg_is_importable(self):
        """`ok/alas/emulator_windows.py` imports it at module level, uncaught."""
        import winreg  # noqa: F401

    def test_winreg_calls_raise_the_missing_key_error(self):
        """Callers guard registry lookups two different ways, and both must catch.

        `except OSError` (ok-ww `config.py`, `emulator_windows.py:34,50`) and
        `except FileNotFoundError` (`emulator_windows.py`, eleven lookups). Only
        `FileNotFoundError` satisfies both -- a bare `OSError` escapes all eleven -- and it
        is what real winreg raises for a missing key. `NotImplementedError` escapes both.
        """
        import winreg

        for call in (lambda: winreg.OpenKey(0, r'Software\Microsoft'),
                     lambda: winreg.QueryValueEx(0, 'InstallPath')):
            with self.assertRaises(OSError):
                call()
            with self.assertRaises(FileNotFoundError):
                call()

    def test_emulator_registry_scan_reports_nothing_rather_than_escaping(self):
        """The end-to-end shape of the bug a bare OSError caused: every lookup in
        `all_emulator_instances` is wrapped in `except FileNotFoundError`."""
        from ok.alas.emulator_windows import EmulatorManager

        self.assertEqual([], EmulatorManager().all_emulator_instances)

    def test_winreg_constants_are_real_integers(self):
        """Callers combine them: `KEY_READ | KEY_WOW64_64KEY` is a TypeError on stubs."""
        import winreg

        self.assertEqual(0x20019, winreg.KEY_READ)
        self.assertEqual(0x100, winreg.KEY_WOW64_64KEY)
        self.assertEqual(0x200, winreg.KEY_WOW64_32KEY)
        # CPython's winreg exposes the HKEY roots unsigned; pywin32 spells them signed.
        self.assertEqual(0x80000001, winreg.HKEY_CURRENT_USER)
        self.assertEqual(0x80000002, winreg.HKEY_LOCAL_MACHINE)
        self.assertEqual(0x20119, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)

    def test_makelong_is_implemented_not_stubbed(self):
        """A C macro on the hot input path, not an OS call."""
        import win32api

        self.assertEqual((4 << 16) | 3, win32api.MAKELONG(3, 4))
        self.assertEqual(0, win32api.MAKELONG(0, 0))
        # packs into the low/high 16 bits, discarding overflow, as the macro does
        self.assertEqual((1 << 16) | 0xFFFF, win32api.MAKELONG(0xFFFF, 1))

    def test_attribute_access_is_memoised(self):
        """`ctypes.windll.user32` must be one object, as in real ctypes -- otherwise
        `patch('pkg.ctypes.windll.user32.GetDpiForWindow')` patches a throwaway.
        """
        import ctypes

        self.assertIs(ctypes.windll.user32, ctypes.windll.user32)
        ctypes.windll.user32.GetDpiForWindow = lambda hwnd: 144
        try:
            self.assertEqual(144, ctypes.windll.user32.GetDpiForWindow(0))
        finally:
            del ctypes.windll.user32.GetDpiForWindow


@skip_on_windows
class TestDeviceLayerImports(unittest.TestCase):
    """Phase 1's real exit criterion, in miniature.

    `import ok` alone is a false green -- `ok/__init__.py` is PEP-562 lazy, so it
    succeeds on a wholly unported tree. What matters is that the lazily-mapped names
    resolve.
    """

    DEVICE_MODULES = (
        'ok.util.window',
        'ok.device.capture_methods',
        'ok.device.interaction_methods',
        'ok.device.capture',
        'ok.device.interaction',
        'ok.device.DeviceManager',
    )

    def test_device_layer_imports(self):
        for name in self.DEVICE_MODULES:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_every_lazy_import_resolves(self):
        import ok

        failures = []
        for name, (module, attr) in sorted(ok._LAZY_IMPORTS.items()):
            try:
                getattr(importlib.import_module(module), attr)
            except Exception as exc:  # noqa: BLE001 - collecting a report
                failures.append(f'{name} -> {module}.{attr}: {type(exc).__name__}: {exc}')
        self.assertEqual([], failures)


@skip_on_windows
class TestGeometryExtraction(unittest.TestCase):
    """`get_crop_point` moved out of the win32-flavoured `bitblt_utils`."""

    def test_bitblt_utils_still_re_exports(self):
        from ok.device.capture_methods import bitblt_utils, geometry

        self.assertIs(geometry.get_crop_point, bitblt_utils.get_crop_point)
        self.assertIs(geometry.parse_reg_flag, bitblt_utils.parse_reg_flag)

    def test_geometry_module_pulls_in_no_windows_code(self):
        source = (REPO / 'ok' / 'device' / 'capture_methods' / 'geometry.py')
        tree = ast.parse(source.read_text(encoding='utf-8'))
        imported = [n for n in ast.walk(tree)
                    if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual([], imported, 'geometry.py must stay dependency-free')

    def test_crop_point_asymmetry_is_preserved(self):
        from ok.device.capture_methods.geometry import get_crop_point

        # x is the horizontal border; y is everything left over vertically (the title
        # bar), NOT a centred split. A 1936x1119 frame around a 1920x1080 client:
        self.assertEqual((8, 31), get_crop_point(1936, 1119, 1920, 1080))
        self.assertEqual((0, 0), get_crop_point(1920, 1080, 1920, 1080))

    def test_parse_reg_flag(self):
        from ok.device.capture_methods.geometry import parse_reg_flag

        self.assertIs(True, parse_reg_flag('a=1;b=2', 'a'))
        self.assertIs(False, parse_reg_flag('a=2', 'a'))
        self.assertIsNone(parse_reg_flag('a=x', 'a'))
        self.assertIsNone(parse_reg_flag('', 'a'))
        self.assertIsNone(parse_reg_flag(None, 'a'))


@skip_on_windows
class TestSingleInstanceLock(unittest.TestCase):
    """POSIX stand-in for the Windows named mutex in `check_mutex`."""

    def setUp(self):
        import tempfile

        self.runtime = tempfile.mkdtemp(prefix='okww-lock-')
        patcher = unittest.mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.runtime})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.name = 'test' + str(os.getpid())

    def test_acquire_then_release_round_trips(self):
        from ok.compat.single_instance import acquire, release

        handle = acquire(self.name)
        self.assertIsNotNone(handle)
        release(handle)

        again = acquire(self.name)
        self.assertIsNotNone(again, 'release must let the next acquire through')
        release(again)

    def test_a_second_holder_is_refused(self):
        """flock binds to the open file description, so this conflicts even in-process --
        matching CreateMutexW reporting ERROR_ALREADY_EXISTS."""
        from ok.compat.single_instance import acquire, release

        held = acquire(self.name)
        self.addCleanup(release, held)
        self.assertIsNone(acquire(self.name))

    def test_lock_lives_under_xdg_runtime_dir_when_there_is_one(self):
        from ok.compat.single_instance import acquire, lock_path, release

        self.assertTrue(lock_path(self.name).startswith(self.runtime))
        handle = acquire(self.name)
        self.addCleanup(release, handle)
        self.assertTrue(os.path.exists(lock_path(self.name)))

    def test_falls_back_to_tempdir_without_xdg_runtime_dir(self):
        import tempfile

        from ok.compat.single_instance import lock_path

        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('XDG_RUNTIME_DIR', None)
            self.assertTrue(lock_path(self.name).startswith(tempfile.gettempdir()))

    def test_check_mutex_takes_the_posix_path_and_is_re_entrant(self):
        import ok.util.process as process

        self.addCleanup(process._release_mutex)
        self.assertTrue(process.check_mutex())
        self.assertIsNotNone(process._mutex_handle)
        self.assertTrue(process.check_mutex(), 'must be re-entrant for the same process')

    def test_a_handle_of_fd_zero_is_still_held_and_still_released(self):
        """`os.open` returns 0 when fd 0 is closed at launch, and 0 is falsy. Testing the
        handle for truthiness short-circuits the re-entrancy check and leaks the lock."""
        import ok.util.process as process

        released = []
        with unittest.mock.patch.object(process, '_mutex_handle', 0), \
                unittest.mock.patch.object(process, '_mutex_owner_file', None), \
                unittest.mock.patch.object(process, '_close_mutex_handle', released.append):
            self.assertTrue(process.check_mutex(), 'fd 0 is a held lock, not a free one')
            process._release_mutex()
        self.assertEqual([0], released, 'fd 0 must be closed, not skipped as falsy')

    def test_an_unopenable_lock_file_is_not_reported_as_a_rival_instance(self):
        """A lock file that cannot be opened says nothing about other instances. Conflating
        it with "somebody holds the lock" sends check_mutex on to terminate a previous
        instance it never established exists."""
        import ok.compat.single_instance as single_instance
        import ok.util.process as process

        with unittest.mock.patch.object(
                single_instance, 'lock_path',
                return_value='/proc/self/no/such/directory/ok.lock'):
            self.assertIs(single_instance.UNAVAILABLE, single_instance.acquire(self.name))

            with unittest.mock.patch.object(
                    process, '_terminate_previous_instances') as terminate:
                self.assertFalse(process.check_mutex(wait_time=0, kill_wait_time=0))
            terminate.assert_not_called()


@skip_on_windows
class TestPosixSignalHandlers(unittest.TestCase):
    """The Linux stand-in for `win32api.SetConsoleCtrlHandler`."""

    def test_handler_maps_signals_to_console_events(self):
        import signal

        import ok

        seen = []
        installed = {}

        class Fake:
            _install_posix_signal_handlers = ok.OK._install_posix_signal_handlers

            def console_handler(self, event):
                seen.append(event)

        original = signal.signal

        def record(sig, handler):
            # Capture only -- never touch this process's real handlers.
            installed[sig] = handler
            return signal.SIG_DFL

        signal.signal = record
        try:
            Fake()._install_posix_signal_handlers()
        finally:
            signal.signal = original

        self.assertEqual({signal.SIGINT, signal.SIGTERM}, set(installed))

        import win32con
        installed[signal.SIGINT](signal.SIGINT, None)
        installed[signal.SIGTERM](signal.SIGTERM, None)
        self.assertEqual([win32con.CTRL_C_EVENT, win32con.CTRL_CLOSE_EVENT], seen)


if __name__ == '__main__':
    unittest.main()
