r"""The `winreg` constants, for Linux.

GENERATED FILE -- do not edit by hand. Regenerate with `python3 tools/gen_win32con.py`,
which transcribes the values out of pywin32 311's `win32/lib/win32con.py`.

`ok/compat/win32_stub.py` binds these onto the stub `winreg` module. They have to be real
integers for the same reason `win32con`'s do: callers combine them, e.g.
`winreg.KEY_READ | winreg.KEY_WOW64_64KEY` in ok-ww's game-install detection, which raises
`TypeError` against a stub. The functions still raise `OSError` -- there is no registry
here -- which is the "nothing registered" answer callers already handle.

HKEY_* are normalised to unsigned 32-bit to match CPython's `winreg`, which exposes them
as e.g. 0x80000001 where pywin32 uses the signed spelling.
"""

HKEY_CLASSES_ROOT = 0x80000000
HKEY_CURRENT_CONFIG = 0x80000005
HKEY_CURRENT_USER = 0x80000001
HKEY_DYN_DATA = 0x80000006
HKEY_LOCAL_MACHINE = 0x80000002
HKEY_PERFORMANCE_DATA = 0x80000004
HKEY_PERFORMANCE_NLSTEXT = 0x80000060
HKEY_PERFORMANCE_TEXT = 0x80000050
HKEY_USERS = 0x80000003
KEY_ALL_ACCESS = 0xf003f
KEY_CREATE_LINK = 0x20
KEY_CREATE_SUB_KEY = 4
KEY_ENUMERATE_SUB_KEYS = 8
KEY_EXECUTE = 0x20019
KEY_NOTIFY = 0x10
KEY_QUERY_VALUE = 1
KEY_READ = 0x20019
KEY_SET_VALUE = 2
KEY_WOW64_32KEY = 0x200
KEY_WOW64_64KEY = 0x100
KEY_WOW64_RES = 0x300
KEY_WRITE = 0x20006
REG_BINARY = 3
REG_DWORD = 4
REG_DWORD_BIG_ENDIAN = 5
REG_DWORD_LITTLE_ENDIAN = 4
REG_EXPAND_SZ = 2
REG_FULL_RESOURCE_DESCRIPTOR = 9
REG_LINK = 6
REG_MULTI_SZ = 7
REG_NONE = 0
REG_NOTIFY_CHANGE_ATTRIBUTES = 2
REG_NOTIFY_CHANGE_SECURITY = 8
REG_QWORD = 11
REG_QWORD_LITTLE_ENDIAN = 11
REG_RESOURCE_LIST = 8
REG_RESOURCE_REQUIREMENTS_LIST = 10
REG_SZ = 1
