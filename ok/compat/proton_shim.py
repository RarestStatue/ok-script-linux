"""Find the game's Proton prefix, put ``okww-input-shim.exe`` in it, and talk to it.

This is the Linux half of PORT.md Phase 4: everything between "the user launched Wuthering
Waves through Steam" and "a socket that accepts ``KEYDOWN 65``".
``ok/device/interaction_methods/wine_post_message.py`` is the only caller.

The split is deliberate: this module knows about Steam, Proton, sockets and processes and
nothing about ok-script's device layer, so it can be tested against a fabricated Steam tree
with no game, no Wine and no display -- which is what ``tests/test_wine_post_message.py``
does for the parsing and command-building half.

Four things here are load-bearing and were each verified on the target machine:

* **The shim must join the game's own wineserver.** ``PostMessage`` cannot cross prefixes
  or wineserver sessions. Separate ``proton run`` invocations against one prefix share a
  session and deliver messages between themselves [PORT.md V10].
* **``$PROTON_DIR`` comes from that prefix's own ``config_info``, never from a default.**
  Launching with a *different* Proton build than the game was last run with rewrites the
  prefix ("Upgrading prefix from X to Y"). ``config_info``'s first line is the build name
  and the rest are absolute paths inside the tool directory, so the tool directory is
  recovered by walking up from one of them to the directory that holds ``proton``. Users
  run dwproton, GE-Proton, Valve Proton and proton-cachyos, and the paths contain spaces.
* **The game is launched by Steam inside the SteamLinuxRuntime container** (its
  ``toolmanifest.vdf`` declares ``require_tool_appid 4183110`` [PORT.md V9]). Whether a
  host-side ``proton run`` still reaches that wineserver is [GATE-1b]; both launch shapes
  are implemented and tried in order, so the answer changes which one wins at runtime and
  nothing else.
* **The shim reports over its socket and the handshake file, never stdout** -- ``proton
  run`` swallows stdout [PORT.md V12]. A shim that dies on startup is detected as the
  absence of a complete handshake file, not as an exit code.
"""

import hashlib
import os
import re
import shutil
import socket
import subprocess
import time

from ok.util.logger import Logger

logger = Logger.get_logger(__name__)

WUWA_APPID = '3513350'
WUWA_EXE = 'Client-Win64-Shipping.exe'

# The shim's own names inside the prefix. `drive_c` is the one directory both sides can
# address: the Linux side by path, the shim as `C:\`.
SHIM_EXE_NAME = 'okww-input-shim.exe'
SHIM_EXE_WINPATH = 'C:\\' + SHIM_EXE_NAME
HANDSHAKE_NAME = 'okww-shim.port'
HANDSHAKE_WINPATH = 'C:\\' + HANDSHAKE_NAME

# `require_tool_appid 4183110` -> SteamLinuxRuntime_4. Newer runtimes get a new appid and a
# new directory name, so the directory is searched for by prefix and the newest wins.
RUNTIME_DIR_PREFIX = 'SteamLinuxRuntime'


def steam_appid(default=WUWA_APPID):
    """The appid to attach to. `OKWW_STEAM_APPID` overrides it for a different install."""
    return os.environ.get('OKWW_STEAM_APPID') or default


class ShimError(Exception):
    """Anything that stops the shim being reachable. Always actionable in its message."""


# --------------------------------------------------------------------- Steam ----

def steam_root_candidates(home=None, environ=None):
    """Every plausible Steam root, de-duplicated by real path, most standard first.

    ``~/.steam/steam`` is a symlink to ``~/.local/share/Steam`` on a normal install, so
    without the ``realpath`` de-duplication every library scan below runs twice.
    """
    environ = os.environ if environ is None else environ
    home = home or os.path.expanduser('~')
    raw = [
        environ.get('STEAM_ROOT'),
        environ.get('STEAM_BASE_FOLDER'),
        os.path.join(home, '.steam', 'steam'),
        os.path.join(home, '.steam', 'root'),
        os.path.join(home, '.local', 'share', 'Steam'),
        # Flatpak Steam.
        os.path.join(home, '.var', 'app', 'com.valvesoftware.Steam', '.local', 'share', 'Steam'),
        os.path.join(home, '.var', 'app', 'com.valvesoftware.Steam', 'data', 'Steam'),
    ]
    seen, roots = set(), []
    for path in raw:
        if not path:
            continue
        real = os.path.realpath(path)
        if real in seen or not os.path.isdir(os.path.join(real, 'steamapps')):
            continue
        seen.add(real)
        roots.append(real)
    return roots


def parse_library_folders(text):
    """The ``"path"`` entries of ``libraryfolders.vdf``, in file order.

    A hand-rolled reader rather than a VDF parser: the file's one interesting key is
    ``path``, the format has been stable for years, and a dependency for this would have to
    be carried into the Linux package.
    """
    return [match.group(1).replace('\\\\', '/')
            for match in re.finditer(r'"path"\s+"([^"]+)"', text)]


def steam_libraries(steam_root):
    """Library directories of one Steam root: the root itself plus its VDF entries."""
    libraries, seen = [], set()
    for path in [steam_root] + _vdf_libraries(steam_root):
        real = os.path.realpath(path)
        if real in seen or not os.path.isdir(os.path.join(real, 'steamapps')):
            continue
        seen.add(real)
        libraries.append(real)
    return libraries


def _vdf_libraries(steam_root):
    vdf = os.path.join(steam_root, 'steamapps', 'libraryfolders.vdf')
    try:
        with open(vdf, 'r', encoding='utf-8', errors='replace') as handle:
            return parse_library_folders(handle.read())
    except OSError:
        return []


def parse_app_manifest(text):
    """``appmanifest_<appid>.acf`` as a flat dict of its top-level string pairs."""
    return {match.group(1): match.group(2)
            for match in re.finditer(r'"([^"]+)"\s+"([^"]*)"', text)}


def proton_dir_from_config_info(text):
    """The Proton tool directory a prefix was last run with, from its ``config_info``.

    Line 1 is the build name and the rest are absolute paths *inside* the tool directory
    (``files/share/fonts/``, ``files/lib/``), so the tool directory is the nearest ancestor
    of any of them that contains the ``proton`` script. Deriving it by stripping a fixed
    suffix breaks the moment a build ships a different first path.
    """
    for line in text.splitlines()[1:]:
        candidate = line.strip().rstrip('/')
        if not candidate.startswith('/'):
            continue
        while candidate and candidate != '/':
            if os.path.isfile(os.path.join(candidate, 'proton')):
                return candidate
            candidate = os.path.dirname(candidate)
    return None


def steam_client_install_path_from_config_info(text):
    """``STEAM_COMPAT_CLIENT_INSTALL_PATH``, which ``config_info`` states outright."""
    for line in text.splitlines()[1:]:
        candidate = line.strip().rstrip('/')
        if candidate.startswith('/') and os.path.isdir(os.path.join(candidate, 'steamapps')):
            return candidate
    return None


class SteamGame:
    """Where one appid's install, prefix and Proton build actually are."""

    def __init__(self, appid, steam_root, library, install_dir, exe_path, compatdata,
                 proton_dir, client_install_path, runtime_entry_point=None):
        self.appid = appid
        self.steam_root = steam_root
        self.library = library
        self.install_dir = install_dir
        self.exe_path = exe_path
        self.compatdata = compatdata
        self.proton_dir = proton_dir
        self.client_install_path = client_install_path
        self.runtime_entry_point = runtime_entry_point

    @property
    def drive_c(self):
        return os.path.join(self.compatdata, 'pfx', 'drive_c')

    @property
    def handshake_path(self):
        return os.path.join(self.drive_c, HANDSHAKE_NAME)

    def __str__(self):
        return (f'SteamGame({self.appid} install={self.install_dir!r} '
                f'proton={os.path.basename(self.proton_dir or "?")!r})')


def find_runtime_entry_point(libraries):
    """``_v2-entry-point`` of the newest installed SteamLinuxRuntime, or None."""
    found = []
    for library in libraries:
        common = os.path.join(library, 'steamapps', 'common')
        try:
            names = os.listdir(common)
        except OSError:
            continue
        for name in names:
            if not name.startswith(RUNTIME_DIR_PREFIX):
                continue
            entry = os.path.join(common, name, '_v2-entry-point')
            if os.path.isfile(entry):
                found.append((name, entry))
    if not found:
        return None
    return sorted(found)[-1][1]


def resolve_steam_game(appid=WUWA_APPID, exe_name=WUWA_EXE, environ=None):
    """Locate the installed game, its prefix and its Proton build.

    Raises ``ShimError`` with an actionable message rather than returning a half-filled
    object: every caller needs all of it.
    """
    roots = steam_root_candidates(environ=environ)
    if not roots:
        raise ShimError('no Steam installation found; the Linux input backend attaches to '
                        'a game launched through Steam')

    for steam_root in roots:
        libraries = steam_libraries(steam_root)
        for library in libraries:
            manifest = os.path.join(library, 'steamapps', f'appmanifest_{appid}.acf')
            compatdata = os.path.join(library, 'steamapps', 'compatdata', appid)
            if not os.path.isfile(manifest):
                continue
            try:
                with open(manifest, 'r', encoding='utf-8', errors='replace') as handle:
                    install_dir = parse_app_manifest(handle.read()).get('installdir')
            except OSError as e:
                raise ShimError(f'cannot read {manifest}: {e}')
            if not install_dir:
                continue
            if not os.path.isdir(os.path.join(compatdata, 'pfx')):
                raise ShimError(f'appid {appid} is installed but has no Proton prefix at '
                                f'{compatdata}; launch it once through Steam')

            config_info = os.path.join(compatdata, 'config_info')
            try:
                with open(config_info, 'r', encoding='utf-8', errors='replace') as handle:
                    info = handle.read()
            except OSError as e:
                raise ShimError(f'cannot read {config_info}: {e}; launch the game once '
                                f'through Steam so Proton records its build')
            proton_dir = proton_dir_from_config_info(info)
            if not proton_dir:
                raise ShimError(f'{config_info} names no Proton build directory; its first '
                                f'line is {info.splitlines()[:1]}')

            return SteamGame(
                appid=appid,
                steam_root=steam_root,
                library=library,
                install_dir=install_dir,
                exe_path=_find_game_exe(library, install_dir, exe_name),
                compatdata=compatdata,
                proton_dir=proton_dir,
                client_install_path=(steam_client_install_path_from_config_info(info)
                                     or steam_root),
                runtime_entry_point=find_runtime_entry_point(libraries),
            )

    raise ShimError(f'appid {appid} is not installed in any Steam library '
                    f'({", ".join(roots)}); launch the game once through Steam')


def _find_game_exe(library, install_dir, exe_name):
    """The game binary, by its usual path first and a bounded walk second."""
    base = os.path.join(library, 'steamapps', 'common', install_dir)
    usual = os.path.join(base, 'Client', 'Binaries', 'Win64', exe_name)
    if os.path.isfile(usual):
        return usual
    for current, dirs, files in os.walk(base):
        if exe_name in files:
            return os.path.join(current, exe_name)
        if current[len(base):].count(os.sep) >= 4:
            dirs[:] = []
    return None


def game_pid(exe_name=WUWA_EXE):
    """The pid of the running game, or None. Cheap enough to call on every retry."""
    try:
        import psutil
    except ImportError:
        return None
    lowered = exe_name.lower()
    for process in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if (process.info.get('name') or '').lower() == lowered:
                return process.info['pid']
            cmdline = process.info.get('cmdline') or []
            if cmdline and cmdline[0].lower().replace('\\', '/').endswith(lowered):
                return process.info['pid']
        except Exception:
            continue
    return None


# ----------------------------------------------------------------- handshake ----

class Handshake:
    """What the shim wrote to ``drive_c/okww-shim.port``: how to reach it, and who it is."""

    def __init__(self, port, token, pid=0, hwnd=0, status=''):
        self.port = port
        self.token = token
        self.pid = pid
        self.hwnd = hwnd
        self.status = status

    def __str__(self):
        return f'Handshake(port={self.port} pid={self.pid} hwnd={self.hwnd} {self.status})'


def parse_handshake(text):
    """A complete handshake, or None while the file is empty, partial or pre-created.

    The Linux side creates this file itself, with mode 0600, *before* launching the shim,
    so the token is never world-readable for even an instant. That means "the file exists"
    proves nothing; "the file parses and carries a status" is the readiness signal.
    """
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition('=')
        if sep:
            fields[key.strip()] = value.strip()
    if not fields.get('status') or not fields.get('port') or not fields.get('token'):
        return None
    try:
        return Handshake(port=int(fields['port']), token=fields['token'],
                         pid=int(fields.get('pid', 0)), hwnd=int(fields.get('hwnd', 0)),
                         status=fields['status'])
    except ValueError:
        return None


def read_handshake(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            return parse_handshake(handle.read())
    except OSError:
        return None


def create_handshake_placeholder(path):
    """Truncate the handshake file to empty at mode 0600, creating it if needed."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


# -------------------------------------------------------------------- launch ----

def find_shim_exe(explicit=None):
    """The built shim, searched in the order that lets a developer override it.

    It ships in ok-ww's repo (``shim/okww-input-shim.exe``) rather than in this library,
    because that is where its source lives and where the app is packaged from.
    """
    candidates = [explicit, os.environ.get('OKWW_INPUT_SHIM')]
    try:
        from ok.util.file import get_path_relative_to_exe
        candidates.append(get_path_relative_to_exe('shim', SHIM_EXE_NAME))
    except Exception:
        pass
    candidates.append(os.path.join(os.getcwd(), 'shim', SHIM_EXE_NAME))
    package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidates.append(os.path.join(package_root, 'shim', SHIM_EXE_NAME))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _digest(path):
    with open(path, 'rb') as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def install_shim(game, shim_exe):
    """Copy the shim into the prefix's ``drive_c``, if it is not already the same file."""
    target = os.path.join(game.drive_c, SHIM_EXE_NAME)
    if os.path.isfile(target):
        try:
            if _digest(target) == _digest(shim_exe):
                return target
        except OSError:
            pass
    os.makedirs(game.drive_c, exist_ok=True)
    shutil.copy2(shim_exe, target)
    logger.info(f'installed the input shim into {target}')
    return target


def shim_argv(exe_name=WUWA_EXE, hwnd_class='UnrealWindow', idle_exit=600):
    """The shim's own arguments -- identical in both launch shapes."""
    return [SHIM_EXE_WINPATH,
            '--exe', exe_name,
            '--class', hwnd_class or '-',
            '--handshake', HANDSHAKE_WINPATH,
            '--idle-exit', str(idle_exit)]


def proton_command(game, argv):
    """The direct host-side launch: ``proton run C:\\okww-input-shim.exe ...``."""
    return [os.path.join(game.proton_dir, 'proton'), 'run'] + argv


def container_command(game, argv):
    """The SteamLinuxRuntime launch, which puts the shim in the game's own container.

    This is [GATE-1b]'s fallback and may be the primary path: the game itself is launched
    inside pressure-vessel, and a host-side ``proton run`` might not see the same
    ``wineserver`` socket.
    """
    if not game.runtime_entry_point:
        return None
    return [game.runtime_entry_point, '--verb=run', '--',
            os.path.join(game.proton_dir, 'proton'), 'run'] + argv


def launch_env(game, environ=None):
    env = dict(os.environ if environ is None else environ)
    env['STEAM_COMPAT_DATA_PATH'] = game.compatdata
    env['STEAM_COMPAT_CLIENT_INSTALL_PATH'] = game.client_install_path
    # Proton refuses to run without this when the prefix lives outside the client install.
    env.setdefault('STEAM_COMPAT_MOUNTS', game.library)
    return env


def wait_for_handshake(path, timeout, poll=0.25, sleep=time.sleep):
    deadline = time.monotonic() + timeout
    while True:
        handshake = read_handshake(path)
        if handshake:
            return handshake
        if time.monotonic() >= deadline:
            return None
        sleep(poll)


def start_shim(game, shim_exe=None, exe_name=WUWA_EXE, hwnd_class='UnrealWindow',
               timeout=25.0, runner=subprocess.Popen, idle_exit=600):
    """Launch the shim into the game's prefix and return its handshake.

    Tries the direct ``proton run`` first and the container entry point second, exactly as
    PORT.md §4b requires -- the user is never asked to choose. Returns
    ``(handshake, process, shape)``.
    """
    shim_exe = find_shim_exe(shim_exe)
    if not shim_exe:
        raise ShimError(f'{SHIM_EXE_NAME} was not found; build it with '
                        f'`x86_64-w64-mingw32-gcc -O2 -s -o shim/{SHIM_EXE_NAME} '
                        f'shim/okww-input-shim.c -lws2_32` or set OKWW_INPUT_SHIM')
    install_shim(game, shim_exe)

    argv = shim_argv(exe_name=exe_name, hwnd_class=hwnd_class, idle_exit=idle_exit)
    env = launch_env(game)
    attempts = [('proton run', proton_command(game, argv)),
                ('SteamLinuxRuntime', container_command(game, argv))]

    failures = []
    for shape, command in attempts:
        if not command:
            continue
        create_handshake_placeholder(game.handshake_path)
        logger.info(f'starting the input shim via {shape}: {command}')
        try:
            process = runner(command, env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             cwd=game.proton_dir)
        except OSError as e:
            failures.append(f'{shape}: {e}')
            continue
        handshake = wait_for_handshake(game.handshake_path, timeout)
        if handshake:
            logger.info(f'input shim ready via {shape}: {handshake}')
            return handshake, process, shape
        failures.append(f'{shape}: no handshake within {timeout:.0f}s')
        logger.info(f'{shape} produced no handshake; trying the next launch shape')
        _terminate(process)

    raise ShimError('the input shim never reported a port. ' + '; '.join(failures))


def _terminate(process):
    try:
        process.terminate()
    except Exception:
        pass


# -------------------------------------------------------------------- client ----

class ShimClient:
    """One persistent, authenticated line connection to the shim.

    ``send`` is fire-and-forget and ``request`` is the only thing that waits: the shim
    replies to nothing else, because upstream's ``post()`` swallows every error and no
    caller ever reads a result (``post_message.py:91-97``). A reply per keypress would put
    a round-trip inside the combat loop and inside ``swipe``, which issues up to 100
    ``move()`` calls back to back.
    """

    def __init__(self, port, token, timeout=2.0, host='127.0.0.1'):
        self.port = port
        self.token = token
        self.timeout = timeout
        self.host = host
        self.sock = None
        self.hwnd = 0
        self._buffer = b''

    def connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = sock
        self._buffer = b''
        reply = self.request(f'HELLO {self.token}', 'HELLO')
        self.hwnd = _int_field(reply, 'hwnd')
        return reply

    def close(self):
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            pass

    def send(self, line):
        """Write one command and do not wait. Raises ``ShimError`` if the link is gone."""
        if self.sock is None:
            raise ShimError('not connected')
        try:
            # `request` leaves a short deadline on the socket; restore the full one, or a
            # later write could time out against whatever was left of the last read.
            self.sock.settimeout(self.timeout)
            self.sock.sendall(line.encode('ascii', 'replace') + b'\n')
        except OSError as e:
            self.close()
            raise ShimError(f'shim write failed: {e}')

    def request(self, line, tag, timeout=None):
        """Write one command and read its tagged reply.

        Replies carry their command name, so a reply that arrived late -- or an
        unsolicited ``ERR`` -- is discarded here instead of being paired with the next
        question.
        """
        self.send(line)
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            reply = self._read_line(deadline)
            head, _, rest = reply.partition(' ')
            if head == tag:
                return rest
            if head == 'ERR':
                err_tag, _, message = rest.partition(' ')
                if err_tag == tag:
                    raise ShimError(f'{tag}: {message}')
            logger.debug(f'discarding an out-of-band shim reply: {reply!r}')

    def _read_line(self, deadline):
        while b'\n' not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ShimError('shim did not reply in time')
            if self.sock is None:
                raise ShimError('not connected')
            try:
                self.sock.settimeout(remaining)
                chunk = self.sock.recv(4096)
            except OSError as e:
                self.close()
                raise ShimError(f'shim read failed: {e}')
            if not chunk:
                self.close()
                raise ShimError('the shim closed the connection')
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b'\n')
        return line.decode('ascii', 'replace').strip()


def _int_field(text, name):
    match = re.search(rf'\b{name}=(-?\d+)', text or '')
    return int(match.group(1)) if match else 0


def connect_or_start(game, shim_exe=None, exe_name=WUWA_EXE, hwnd_class='UnrealWindow',
                     timeout=25.0):
    """Reuse a live shim if one is already in the prefix, otherwise launch one.

    Reconnecting matters more than it looks: ok-ww restarts its device layer whenever the
    user switches capture or interaction backend in the GUI, and relaunching the shim each
    time would leave one orphan per switch until their idle timeouts expired.
    """
    handshake = read_handshake(game.handshake_path)
    if handshake:
        client = ShimClient(handshake.port, handshake.token)
        try:
            client.connect()
            logger.info(f'reusing the input shim already in the prefix: {handshake}')
            return client, None
        except (OSError, ShimError) as e:
            logger.info(f'the recorded shim at port {handshake.port} did not answer ({e}); '
                        f'starting a new one')
            client.close()

    handshake, process, _shape = start_shim(game, shim_exe=shim_exe, exe_name=exe_name,
                                            hwnd_class=hwnd_class, timeout=timeout)
    client = ShimClient(handshake.port, handshake.token)
    client.connect()
    return client, process
