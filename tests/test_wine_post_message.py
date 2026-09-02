"""Phase 4: the Wine input path, everything that does not need Wine.

Three layers are covered here, and each one has failed silently at least once in this port:

* **Steam/Proton resolution** -- the parsing of `libraryfolders.vdf`, `appmanifest_*.acf`
  and `config_info`, against a fabricated Steam tree with a space in the Proton build name,
  because the real one on the target machine has one (`DW-Proton Latest`).
* **The socket protocol** -- against a real loopback server that speaks the shim's side of
  it, so the reply tagging, the token handshake and the fire-and-forget rule are exercised
  end to end rather than mocked.
* **`WinePostMessageInteraction`'s semantics** -- the `-1` mouse branch, the two upstream
  bugs the port fixes, the `try_activate` call sites, and the rule that the hot path never
  waits for a reply. All of those are invisible to any test that only checks that a
  command was sent.
"""

import os
import socket
import threading
import time
import unittest

from ok.compat.proton_shim import (
    Handshake, ShimClient, ShimError, container_command, create_handshake_placeholder,
    find_runtime_entry_point, launch_env, parse_app_manifest, parse_handshake,
    parse_library_folders, proton_command, proton_dir_from_config_info, resolve_steam_game,
    shim_argv, start_shim, steam_client_install_path_from_config_info, steam_libraries,
    steam_root_candidates,
)
from ok.device.interaction_methods.keys import vk_key_dict
from ok.device.interaction_methods.wine_post_message import WinePostMessageInteraction

APPID = '999999'


def build_steam_tree(root, appid=APPID, proton_name='DW-Proton Latest', with_runtime=True):
    """A Steam install that looks like the real one, including the space in the build name."""
    steam = os.path.join(root, 'Steam')
    library = os.path.join(root, 'games', 'SteamLibrary')
    proton_dir = os.path.join(steam, 'compatibilitytools.d', proton_name, 'files')
    os.makedirs(os.path.join(steam, 'steamapps', 'common'), exist_ok=True)
    os.makedirs(os.path.join(library, 'steamapps', 'common', 'Wuthering Waves',
                             'Client', 'Binaries', 'Win64'), exist_ok=True)
    compatdata = os.path.join(library, 'steamapps', 'compatdata', appid)
    os.makedirs(os.path.join(compatdata, 'pfx', 'drive_c'), exist_ok=True)
    os.makedirs(proton_dir, exist_ok=True)

    with open(os.path.join(proton_dir, 'proton'), 'w') as handle:
        handle.write('#!/bin/sh\n')
    with open(os.path.join(steam, 'steamapps', 'libraryfolders.vdf'), 'w') as handle:
        handle.write('"libraryfolders"\n{\n'
                     f'\t"0"\n\t{{\n\t\t"path"\t\t"{steam}"\n\t}}\n'
                     f'\t"1"\n\t{{\n\t\t"path"\t\t"{library}"\n\t}}\n}}\n')
    with open(os.path.join(library, 'steamapps', f'appmanifest_{appid}.acf'), 'w') as handle:
        handle.write('"AppState"\n{\n\t"appid"\t\t"%s"\n\t"installdir"\t\t"Wuthering Waves"\n}\n'
                     % appid)
    with open(os.path.join(compatdata, 'config_info'), 'w') as handle:
        handle.write(f'proton-test-1.0\n{proton_dir}/share/fonts/\n{proton_dir}/lib/\n{steam}\n')
    exe = os.path.join(library, 'steamapps', 'common', 'Wuthering Waves', 'Client',
                       'Binaries', 'Win64', 'Client-Win64-Shipping.exe')
    with open(exe, 'w') as handle:
        handle.write('')
    if with_runtime:
        runtime = os.path.join(steam, 'steamapps', 'common', 'SteamLinuxRuntime_4')
        os.makedirs(runtime, exist_ok=True)
        with open(os.path.join(runtime, '_v2-entry-point'), 'w') as handle:
            handle.write('#!/bin/sh\n')
    return steam, library, proton_dir, compatdata, exe


class TestSteamResolution(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (self.steam, self.library, self.proton_dir,
         self.compatdata, self.exe) = build_steam_tree(self.tmp.name)
        self.environ = {'STEAM_ROOT': self.steam}

    def test_library_folders_are_read_in_file_order(self):
        text = '"libraryfolders"\n{\n"0"\n{\n"path"\t"/a"\n}\n"1"\n{\n"path"\t"/b/c"\n}\n}\n'
        self.assertEqual(['/a', '/b/c'], parse_library_folders(text))

    def test_a_windows_style_escaped_path_is_unescaped(self):
        self.assertEqual(['D:/SteamLibrary'],
                         parse_library_folders('"path"\t"D:\\\\SteamLibrary"'))

    def test_the_app_manifest_yields_the_install_dir(self):
        manifest = '"AppState"\n{\n"appid" "3513350"\n"installdir" "Wuthering Waves"\n}\n'
        self.assertEqual('Wuthering Waves', parse_app_manifest(manifest)['installdir'])

    def test_the_proton_dir_is_found_by_walking_up_to_the_proton_script(self):
        """Not by stripping `files/share/fonts/`: builds differ in which paths they list."""
        text = (f'dwproton-11.0-12\n{self.proton_dir}/share/fonts/\n'
                f'{self.proton_dir}/lib/\n{self.steam}\n')
        self.assertEqual(self.proton_dir, proton_dir_from_config_info(text))

    def test_the_proton_dir_survives_a_build_that_lists_a_different_first_path(self):
        text = f'some-proton\n{self.proton_dir}/lib/\n{self.steam}\n'
        self.assertEqual(self.proton_dir, proton_dir_from_config_info(text))

    def test_config_info_hands_over_the_steam_client_install_path(self):
        text = (f'dwproton\n{self.proton_dir}/share/fonts/\n{self.steam}\n')
        self.assertEqual(self.steam, steam_client_install_path_from_config_info(text))

    def test_a_config_info_naming_no_proton_build_is_reported_not_guessed(self):
        self.assertIsNone(proton_dir_from_config_info('name\n/nonexistent/place/\n'))

    def test_steam_roots_are_de_duplicated_by_real_path(self):
        link = os.path.join(self.tmp.name, 'steam-symlink')
        os.symlink(self.steam, link)
        roots = steam_root_candidates(home=self.tmp.name,
                                      environ={'STEAM_ROOT': self.steam,
                                               'STEAM_BASE_FOLDER': link})
        self.assertEqual([os.path.realpath(self.steam)], roots)

    def test_libraries_include_the_root_and_the_vdf_entries(self):
        self.assertEqual([os.path.realpath(self.steam), os.path.realpath(self.library)],
                         steam_libraries(self.steam))

    def test_resolve_finds_the_install_the_prefix_and_the_build(self):
        game = resolve_steam_game(appid=APPID, environ=self.environ)
        self.assertEqual(os.path.realpath(self.library), game.library)
        self.assertEqual('Wuthering Waves', game.install_dir)
        self.assertEqual(os.path.realpath(self.exe), os.path.realpath(game.exe_path))
        self.assertEqual(self.proton_dir, game.proton_dir)
        self.assertEqual(self.steam, game.client_install_path)
        self.assertTrue(game.handshake_path.endswith('pfx/drive_c/okww-shim.port'))

    def test_the_runtime_entry_point_is_found_for_the_container_fallback(self):
        game = resolve_steam_game(appid=APPID, environ=self.environ)
        self.assertTrue(game.runtime_entry_point.endswith('SteamLinuxRuntime_4/_v2-entry-point'))
        self.assertIsNone(find_runtime_entry_point([self.library]))

    def test_the_numbered_runtime_wins_over_the_older_named_ones(self):
        """This machine has `_soldier` installed too, and 's' sorts after '4'."""
        common = os.path.join(self.steam, 'steamapps', 'common')
        for name in ('SteamLinuxRuntime_soldier', 'SteamLinuxRuntime_sniper'):
            os.makedirs(os.path.join(common, name), exist_ok=True)
            with open(os.path.join(common, name, '_v2-entry-point'), 'w') as handle:
                handle.write('#!/bin/sh\n')
        entry = find_runtime_entry_point([self.steam])
        self.assertTrue(entry.endswith('SteamLinuxRuntime_4/_v2-entry-point'), entry)

    def test_a_missing_game_says_what_to_do_rather_than_returning_none(self):
        with self.assertRaises(ShimError) as caught:
            resolve_steam_game(appid='424242', environ=self.environ)
        self.assertIn('through Steam', str(caught.exception))

    def test_a_prefix_that_was_never_run_is_reported_as_such(self):
        import shutil
        shutil.rmtree(os.path.join(self.compatdata, 'pfx'))
        with self.assertRaises(ShimError) as caught:
            resolve_steam_game(appid=APPID, environ=self.environ)
        self.assertIn('no Proton prefix', str(caught.exception))

    def test_the_launch_commands_keep_the_space_in_the_build_name_as_one_argument(self):
        game = resolve_steam_game(appid=APPID, environ=self.environ)
        argv = shim_argv(exe_name='Client-Win64-Shipping.exe')
        direct = proton_command(game, argv)
        self.assertEqual(os.path.join(self.proton_dir, 'proton'), direct[0])
        self.assertEqual(['run', 'C:\\okww-input-shim.exe'], direct[1:3])
        self.assertIn('DW-Proton Latest', direct[0])

        container = container_command(game, argv)
        self.assertTrue(container[0].endswith('_v2-entry-point'))
        self.assertEqual(['--verb=run', '--'], container[1:3])
        self.assertEqual(direct, container[3:])

    def test_the_environment_points_proton_at_the_games_own_prefix(self):
        game = resolve_steam_game(appid=APPID, environ=self.environ)
        env = launch_env(game, environ={})
        self.assertEqual(game.compatdata, env['STEAM_COMPAT_DATA_PATH'])
        self.assertEqual(self.steam, env['STEAM_COMPAT_CLIENT_INSTALL_PATH'])


class TestHandshake(unittest.TestCase):

    def test_a_complete_handshake_parses(self):
        handshake = parse_handshake('port=41234\ntoken=abcdef\npid=77\nhwnd=131078\nstatus=ready\n')
        self.assertEqual((41234, 'abcdef', 77, 131078, 'ready'),
                         (handshake.port, handshake.token, handshake.pid, handshake.hwnd,
                          handshake.status))

    def test_the_placeholder_the_linux_side_creates_is_not_mistaken_for_readiness(self):
        """The file exists before the shim runs, so its *existence* proves nothing."""
        self.assertIsNone(parse_handshake(''))
        self.assertIsNone(parse_handshake('port=41234\ntoken=abc\n'))

    def test_a_truncated_line_is_rejected_rather_than_half_read(self):
        self.assertIsNone(parse_handshake('port=4123'))
        self.assertIsNone(parse_handshake('port=notanumber\ntoken=a\nstatus=ready\n'))

    def test_the_placeholder_is_created_private_to_this_user(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'okww-shim.port')
            with open(path, 'w') as handle:  # a stale, world-readable file from before
                handle.write('port=1\ntoken=old\nstatus=ready\n')
            os.chmod(path, 0o644)
            create_handshake_placeholder(path)
            self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
            self.assertEqual('', open(path).read())


class TestLaunchFallback(unittest.TestCase):
    """[GATE-1b]: when the host-side `proton run` produces nothing, the container path runs."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (self.steam, self.library, self.proton_dir,
         self.compatdata, _exe) = build_steam_tree(self.tmp.name)
        self.game = resolve_steam_game(appid=APPID, environ={'STEAM_ROOT': self.steam})
        self.shim = os.path.join(self.tmp.name, 'okww-input-shim.exe')
        with open(self.shim, 'wb') as handle:
            handle.write(b'MZ fake')
        self.commands = []

    def _runner(self, writes_on):
        class FakeProcess:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

        def runner(command, **kwargs):
            self.commands.append(command)
            if writes_on is not None and len(self.commands) == writes_on:
                with open(self.game.handshake_path, 'w') as handle:
                    handle.write('port=45000\ntoken=deadbeef\npid=5\nhwnd=9\nstatus=ready\n')
            return FakeProcess()

        return runner

    def test_the_direct_proton_run_is_tried_first_and_wins_when_it_answers(self):
        handshake, _process, shape = start_shim(self.game, shim_exe=self.shim,
                                                runner=self._runner(1), timeout=1)
        self.assertEqual('proton run', shape)
        self.assertEqual(45000, handshake.port)
        self.assertEqual(1, len(self.commands))

    def test_the_container_entry_point_is_tried_when_the_direct_one_is_silent(self):
        handshake, _process, shape = start_shim(self.game, shim_exe=self.shim,
                                                runner=self._runner(2), timeout=1)
        self.assertEqual('SteamLinuxRuntime', shape)
        self.assertEqual(45000, handshake.port)
        self.assertEqual(2, len(self.commands))
        self.assertTrue(self.commands[1][0].endswith('_v2-entry-point'))

    def test_both_shapes_failing_names_both_in_the_error(self):
        with self.assertRaises(ShimError) as caught:
            start_shim(self.game, shim_exe=self.shim, runner=self._runner(None), timeout=0.4)
        message = str(caught.exception)
        self.assertIn('proton run', message)
        self.assertIn('SteamLinuxRuntime', message)

    def test_the_shim_is_installed_into_drive_c_before_launching(self):
        start_shim(self.game, shim_exe=self.shim, runner=self._runner(1), timeout=1)
        installed = os.path.join(self.game.drive_c, 'okww-input-shim.exe')
        self.assertEqual(b'MZ fake', open(installed, 'rb').read())

    def test_a_stale_handshake_cannot_be_mistaken_for_this_launch(self):
        with open(self.game.handshake_path, 'w') as handle:
            handle.write('port=1\ntoken=stale\nstatus=ready\n')
        with self.assertRaises(ShimError):
            start_shim(self.game, shim_exe=self.shim, runner=self._runner(None), timeout=0.4)


class FakeShimServer:
    """The shim's side of the protocol, in Python, on a real loopback socket.

    Enough of it to test the client for real -- token auth, tagged replies, and above all
    *silence* for the fire-and-forget commands, which is the property that keeps the combat
    loop from blocking.
    """

    REPLYING = {'HELLO', 'PING', 'FINDWIN', 'GEOM', 'GETCURSOR', 'VKKEYSCAN', 'STATS', 'QUIT'}

    def __init__(self, token='tok3n', hwnd=131078):
        self.token = token
        self.hwnd = hwnd
        self.received = []
        self.unsolicited = None
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        buffer = b''
        authed = False
        with conn:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b'\n' in buffer:
                    line, _, buffer = buffer.partition(b'\n')
                    text = line.decode().strip()
                    self.received.append(text)
                    head, _, rest = text.partition(' ')
                    if not authed:
                        if head == 'HELLO' and rest == self.token:
                            authed = True
                            conn.sendall(f'HELLO ok hwnd={self.hwnd}\n'.encode())
                            continue
                        return  # an unauthenticated peer is dropped, silently
                    if self.unsolicited:
                        conn.sendall((self.unsolicited + '\n').encode())
                        self.unsolicited = None
                    if head not in self.REPLYING:
                        continue  # fire-and-forget: no reply at all
                    if head == 'PING':
                        conn.sendall(f'PING pong hwnd={self.hwnd} posts=1 errors=0\n'.encode())
                    elif head == 'FINDWIN':
                        conn.sendall(f'FINDWIN hwnd={self.hwnd}\n'.encode())
                    elif head == 'GEOM':
                        conn.sendall(b'GEOM 0 0 2560 1440\n')
                    elif head == 'GETCURSOR':
                        conn.sendall(b'GETCURSOR 1280 720\n')
                    elif head == 'VKKEYSCAN':
                        conn.sendall(b'VKKEYSCAN 321\n')  # 0x141: 'A', shift in the high byte
                    elif head == 'QUIT':
                        conn.sendall(b'QUIT ok\n')
                        return

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class TestShimClient(unittest.TestCase):

    def setUp(self):
        self.server = FakeShimServer()
        self.addCleanup(self.server.close)

    def test_the_handshake_authenticates_and_reports_the_window(self):
        client = ShimClient(self.server.port, self.server.token)
        self.addCleanup(client.close)
        client.connect()
        self.assertEqual(131078, client.hwnd)

    def test_a_wrong_token_is_dropped_without_telling_the_caller_why(self):
        client = ShimClient(self.server.port, 'wrong', timeout=1)
        self.addCleanup(client.close)
        with self.assertRaises(ShimError):
            client.connect()

    def test_a_request_returns_its_own_tagged_reply(self):
        client = ShimClient(self.server.port, self.server.token)
        self.addCleanup(client.close)
        client.connect()
        self.assertEqual('0 0 2560 1440', client.request('GEOM', 'GEOM'))
        self.assertEqual('1280 720', client.request('GETCURSOR', 'GETCURSOR'))

    def test_an_out_of_band_line_is_discarded_instead_of_answering_the_next_question(self):
        """The reply tag is what keeps a late `ERR` from being read as the next answer."""
        client = ShimClient(self.server.port, self.server.token)
        self.addCleanup(client.close)
        client.connect()
        self.server.unsolicited = 'ERR KEYDOWN notfound'
        self.assertEqual('0 0 2560 1440', client.request('GEOM', 'GEOM'))

    def test_an_error_for_the_command_asked_about_is_raised(self):
        client = ShimClient(self.server.port, self.server.token)
        self.addCleanup(client.close)
        client.connect()
        self.server.unsolicited = 'ERR GEOM notfound'
        with self.assertRaises(ShimError) as caught:
            client.request('GEOM', 'GEOM')
        self.assertIn('notfound', str(caught.exception))

    def test_fire_and_forget_commands_produce_no_reply_to_read(self):
        client = ShimClient(self.server.port, self.server.token)
        self.addCleanup(client.close)
        client.connect()
        for _ in range(200):
            client.send('KEYDOWN 65')
        self.assertEqual('0 0 2560 1440', client.request('GEOM', 'GEOM'))
        self.assertEqual(200, self.server.received.count('KEYDOWN 65'))

    def test_writing_to_a_closed_link_raises_shim_error_not_oserror(self):
        client = ShimClient(self.server.port, self.server.token)
        client.connect()
        client.close()
        with self.assertRaises(ShimError):
            client.send('KEYDOWN 65')


class FakeClient:
    """Records what the interaction would put on the wire."""

    def __init__(self):
        self.sent = []
        self.requests = []
        self.hwnd = 42
        self.replies = {'GETCURSOR': '1280 720', 'GEOM': '0 0 2560 1440',
                        'FINDWIN': 'hwnd=42', 'VKKEYSCAN': '321'}
        self.fail = False

    def send(self, line):
        if self.fail:
            raise ShimError('link down')
        self.sent.append(line)

    def request(self, line, tag):
        if self.fail:
            raise ShimError('link down')
        self.requests.append(line)
        return self.replies[tag]

    def close(self):
        pass

    def commands(self):
        return [line.split(' ')[0] for line in self.sent]


class FakeWindow:
    hwnd = 12345
    top_hwnd = 12345
    hwnds = []
    hwnd_class = 'UnrealWindow'
    exe_names = ['Client-Win64-Shipping.exe']

    def get_top_window_cords(self, x, y):
        return x, y  # top offsets are 0 on Linux [Phase 2]


class TestInteraction(unittest.TestCase):

    def setUp(self):
        self.window = FakeWindow()
        # No connection attempt: the backend would otherwise go looking for Steam.
        original = WinePostMessageInteraction._ensure_connection
        WinePostMessageInteraction._ensure_connection = lambda self: None
        self.addCleanup(setattr, WinePostMessageInteraction, '_ensure_connection', original)
        self.interaction = WinePostMessageInteraction(None, self.window)
        self.client = FakeClient()
        self.interaction._client = self.client

    def test_a_named_key_is_sent_as_its_virtual_key_code(self):
        self.interaction.send_key_down('F1')
        self.interaction.send_key_up('F1')
        self.assertEqual([f'KEYDOWN {vk_key_dict["F1"]}', f'KEYUP {vk_key_dict["F1"]}'],
                         self.client.sent[1:])

    def test_the_virtual_key_table_holds_integers(self):
        """Pins `win32con` to the real constants module.

        With a stubbed `win32con` every value here is a stub object, the shim receives
        garbage, and nothing else in the suite notices [PORT.md V25].
        """
        self.assertIsInstance(vk_key_dict['F1'], int)
        self.assertIsInstance(vk_key_dict['SPACE'], int)

    def test_a_letter_resolves_locally_without_a_round_trip(self):
        self.assertEqual(ord('Q'), self.interaction.get_key_by_str('q'))
        self.assertEqual([], self.client.requests)

    def test_a_symbol_asks_the_prefix_once_and_keeps_only_the_key_code(self):
        """`VkKeyScan` packs the shift state into the high byte; upstream posts it whole."""
        self.assertEqual(0x41, self.interaction.get_key_by_str('!'))
        self.assertEqual(0x41, self.interaction.get_key_by_str('!'))
        self.assertEqual(['VKKEYSCAN 33'], self.client.requests)

    def test_send_key_down_activates_first_and_send_key_up_does_not(self):
        self.interaction.send_key_down('F1')
        self.assertEqual(['ACTIVATE', 'KEYDOWN'], self.client.commands())
        self.client.sent.clear()
        self.interaction.send_key_up('F1')
        self.assertEqual(['KEYUP'], self.client.commands())

    def test_input_text_activates_once_and_posts_one_char_per_character(self):
        self.interaction.input_text('hi')
        self.assertEqual(['ACTIVATE', 'CHAR', 'CHAR'], self.client.commands())
        self.assertEqual([f'CHAR {ord("h")}', f'CHAR {ord("i")}'], self.client.sent[1:])

    def test_a_move_activates_caches_and_posts_client_coordinates(self):
        packed = self.interaction.move(100, 200)
        self.assertEqual((100, 200), self.interaction.bg_mouse_pos)
        self.assertEqual('MOUSEMOVE 100 200 0', self.client.sent[-1])
        self.assertEqual((200 << 16) | 100, packed)

    def test_minus_one_reuses_the_cached_position_without_overwriting_it(self):
        self.interaction.move(100, 200)
        self.client.sent.clear()
        packed = self.interaction.update_mouse_pos(-1, -1)
        self.assertEqual((100, 200), self.interaction.bg_mouse_pos)
        self.assertEqual((200 << 16) | 100, packed)

    def test_click_at_minus_one_presses_where_the_pointer_already_is(self):
        self.interaction.move(640, 360)
        self.client.sent.clear()
        self.interaction.click(-1, -1, move=False, down_time=0)
        self.assertEqual(['ACTIVATE', 'LDOWN 640 360', 'LUP 640 360'], self.client.sent)

    def test_mouse_up_releases_where_the_drag_ended_not_at_the_origin(self):
        """Upstream releases at `self.mouse_pos`, which is `(0, 0)` forever."""
        self.interaction.mouse_down(300, 400)
        self.client.sent.clear()
        self.interaction.mouse_up()
        self.assertEqual(['LUP 300 400'], self.client.sent)

    def test_the_default_swipe_duration_does_not_divide_by_zero(self):
        """Upstream: `steps = int(3 / 100)` == 0, then `dx / steps`."""
        self.interaction.swipe(10, 10, 200, 200)
        moves = [line for line in self.client.sent if line.startswith('MOUSEMOVE')]
        self.assertTrue(moves)
        self.assertEqual('LUP 10 10', self.client.sent[-1])

    def test_a_right_click_posts_the_right_button_pair(self):
        # Upstream's own `right_click` raises AttributeError on the first line
        # (`super().right_click` does not exist); this one works.
        self.interaction.right_click(5, 6)
        self.assertEqual(['ACTIVATE', 'RDOWN 5 6', 'RUP 5 6'], self.client.sent)

    def test_a_middle_click_posts_the_middle_button_pair(self):
        self.interaction.click(7, 8, key='middle', move=False, down_time=0)
        self.assertEqual(['MDOWN 7 8', 'MUP 7 8'], self.client.sent[1:])

    def test_scroll_activates_and_carries_the_wheel_amount(self):
        self.interaction.scroll(10, 20, -3)
        self.assertEqual(['ACTIVATE', 'ACTIVATE', 'WHEEL 10 20 -3'], self.client.sent)

    def test_scroll_without_a_position_posts_at_the_origin_like_upstream(self):
        self.interaction.scroll(0, 0, 1)
        self.assertEqual('WHEEL 0 0 1', self.client.sent[-1])

    def test_nothing_on_the_hot_path_waits_for_a_reply(self):
        self.interaction.send_key('F1', down_time=0)
        self.interaction.move(1, 2)
        self.interaction.click(3, 4, move=False, down_time=0)
        self.interaction.scroll(5, 6, 1)
        self.interaction.input_text('x')
        self.interaction.set_cursor_pos((9, 9))
        self.assertEqual([], self.client.requests)

    def test_the_cursor_is_cached_so_a_2ms_poll_cannot_saturate_the_link(self):
        self.assertEqual((1280, 720), self.interaction.get_cursor_pos())
        for _ in range(50):
            self.interaction.get_cursor_pos()
        self.assertEqual(1, len(self.client.requests))

    def test_setting_the_cursor_updates_the_cache_and_posts_screen_coordinates(self):
        self.interaction.set_cursor_pos((11, 22))
        self.assertEqual('SETCURSOR 11 22', self.client.sent[-1])
        self.assertEqual((11, 22), self.interaction.get_cursor_pos())
        self.assertEqual([], self.client.requests)

    def test_a_dead_link_drops_input_instead_of_raising_into_task_code(self):
        self.client.fail = True
        self.interaction.send_key('F1', down_time=0)
        self.interaction.click(1, 2, move=False, down_time=0)
        self.assertIsNone(self.interaction._client)
        self.assertGreater(self.interaction._dropped, 0)

    def test_a_dead_link_answers_get_cursor_pos_with_the_last_known_value(self):
        self.interaction.set_cursor_pos((5, 5))
        self.interaction._cursor_time = 0
        self.client.fail = True
        self.assertEqual((5, 5), self.interaction.get_cursor_pos())

    def test_geometry_and_window_lookups_are_the_request_response_half(self):
        self.interaction._cursor_time = 0
        self.assertEqual((0, 0, 2560, 1440), self.interaction.get_client_geometry())
        self.assertEqual(42, self.interaction.find_window())
        self.assertEqual(['GEOM', 'FINDWIN'], self.client.requests)

    def test_the_backend_reports_the_window_layers_hwnd_and_always_captures(self):
        self.assertEqual(12345, self.interaction.hwnd)
        self.assertTrue(self.interaction.should_capture())

    def test_destroying_the_backend_asks_the_shim_to_exit(self):
        self.interaction.on_destroy()
        self.assertEqual('QUIT', self.client.sent[-1])
        self.assertIsNone(self.interaction._client)


class TestUpstreamParity(unittest.TestCase):
    """The Linux backend must answer every call site the Windows one does."""

    def test_every_public_method_of_post_message_interaction_exists_here(self):
        from ok.device.interaction_methods.post_message import PostMessageInteraction

        # `post` is the Win32 primitive itself -- `PostMessage(hwnd, msg, w, l)` -- and is
        # replaced here by the socket write, not reimplemented. Nothing outside the class
        # calls it.
        missing = [name for name, value in vars(PostMessageInteraction).items()
                   if not name.startswith('_') and callable(value) and name != 'post'
                   and not hasattr(WinePostMessageInteraction, name)]
        self.assertEqual([], missing)

    def test_the_cursor_api_exists_on_every_backend_through_the_base(self):
        from ok.device.interaction_methods.base import BaseInteraction

        self.assertTrue(hasattr(BaseInteraction, 'get_cursor_pos'))
        self.assertTrue(hasattr(BaseInteraction, 'set_cursor_pos'))
        base = BaseInteraction(None)
        base.set_cursor_pos((3, 4))
        self.assertEqual((3, 4), base.get_cursor_pos())

    def test_the_device_manager_can_select_it_by_name_from_a_config(self):
        """Both ladders: the constructor's and `set_interaction`'s, which the GUI uses."""
        import inspect

        from ok.device import DeviceManager

        source = inspect.getsource(DeviceManager)
        self.assertEqual(2, source.count("== 'WinePostMessage'"))
        self.assertEqual(2, source.count('self.win_interaction_class = WinePostMessageInteraction'))


class TestReconnect(unittest.TestCase):
    """The link comes back on its own, without anyone sending anything.

    ok-ww is normally started *before* the game, so the first connection attempt fails with
    "the game is not running". A backend that only retried on the next `send` would drop
    every keypress of the first seconds of play while `proton run` took its 10-20 s.
    """

    def test_the_maintainer_thread_retries_until_the_game_appears(self):
        from ok.device.interaction_methods import wine_post_message as module

        attempts = []

        def fake_connect(interaction):
            attempts.append(time.monotonic())
            if len(attempts) >= 3:
                interaction._client = FakeClient()

        original_backoff = module.RECONNECT_BACKOFF
        original_connect = module.WinePostMessageInteraction._connect
        module.RECONNECT_BACKOFF = (0.02, 0.02, 0.02, 0.02)
        module.WinePostMessageInteraction._connect = fake_connect
        self.addCleanup(setattr, module, 'RECONNECT_BACKOFF', original_backoff)
        self.addCleanup(setattr, module.WinePostMessageInteraction, '_connect', original_connect)

        interaction = module.WinePostMessageInteraction(None, FakeWindow())
        self.addCleanup(interaction.on_destroy)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not interaction.connected:
            time.sleep(0.02)
        self.assertTrue(interaction.connected)
        self.assertGreaterEqual(len(attempts), 3)

    def test_a_write_failure_drops_the_link_and_asks_for_a_new_one(self):
        from ok.device.interaction_methods import wine_post_message as module

        original = module.WinePostMessageInteraction._ensure_connection
        asked = []
        module.WinePostMessageInteraction._ensure_connection = lambda self: asked.append(1)
        self.addCleanup(setattr, module.WinePostMessageInteraction, '_ensure_connection', original)

        interaction = module.WinePostMessageInteraction(None, FakeWindow())
        client = FakeClient()
        client.fail = True
        interaction._client = client
        interaction.send_key_up('F1')
        self.assertIsNone(interaction._client)
        self.assertTrue(asked)


class TestTiming(unittest.TestCase):

    def test_a_hundred_hot_path_writes_cost_well_under_a_frame(self):
        """`swipe` issues up to 100 `move()` calls back to back; they must not block."""
        server = FakeShimServer()
        self.addCleanup(server.close)
        client = ShimClient(server.port, server.token)
        self.addCleanup(client.close)
        client.connect()
        start = time.monotonic()
        for _ in range(100):
            client.send('MOUSEMOVE 100 100 1')
        self.assertLess(time.monotonic() - start, 0.05)


if __name__ == '__main__':
    unittest.main()
