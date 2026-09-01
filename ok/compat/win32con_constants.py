r"""The subset of `win32con` that ok-script actually references, for Linux.

GENERATED FILE -- do not edit by hand. Regenerate with `python3 tools/gen_win32con.py`,
which transcribes the values out of pywin32 311's `win32/lib/win32con.py`.

Why this is real constants and not a `_Missing` stub: `win32con`'s members are integers
used in bit arithmetic and, critically, as the *values* of `vk_key_dict` in
`ok/device/interaction_methods/keys.py`. A stub does not raise there -- it silently makes
every virtual-key code a stub object, and the input backend then posts garbage.

The name set is exactly what `grep -rhoP "win32con\.\w+" --include=*.py ok` yields
(94 names). Anything else raises `AttributeError` from `__getattr__` naming the
constant, so an upstream rebase that starts using a new one fails loudly here rather than
somewhere far away.
"""

# Clipboard formats
CF_DIB = 8
CF_UNICODETEXT = 13

# Console control events
CTRL_C_EVENT = 0
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

# Window styles / GetWindowLong indices
GWL_EXSTYLE = -0x14
GWL_STYLE = -0x10
WS_CAPTION = 0xc00000
WS_OVERLAPPED = 0
WS_POPUP = -0x80000000
WS_SYSMENU = 0x80000

# GetWindow / ShowWindow / SetWindowPos
GW_HWNDNEXT = 2
GW_OWNER = 4
HWND_NOTOPMOST = -2
HWND_TOPMOST = -1
SWP_FRAMECHANGED = 0x20
SWP_NOMOVE = 2
SWP_NOSIZE = 1
SWP_SHOWWINDOW = 0x40
SW_RESTORE = 9
SW_SHOW = 5

# Icons / images
IDI_APPLICATION = 0x7f00
IMAGE_ICON = 1
LR_DEFAULTSIZE = 0x40
LR_LOADFROMFILE = 0x10

# Mouse key state (wParam of the WM_*BUTTON* / WM_MOUSEMOVE family)
MK_LBUTTON = 1
MK_MBUTTON = 0x10
MK_RBUTTON = 2
WHEEL_DELTA = 0x78

# Monitors / metrics / blitting
MONITOR_DEFAULTTONEAREST = 2
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SRCCOPY = 0xcc0020

# Window messages
WM_ACTIVATE = 6
WM_CHAR = 0x102
WM_CLOSE = 0x10
WM_DESTROY = 2
WM_KEYDOWN = 0x100
WM_KEYUP = 0x101
WM_LBUTTONDOWN = 0x201
WM_LBUTTONUP = 0x202
WM_MBUTTONDOWN = 0x207
WM_MBUTTONUP = 0x208
WM_MOUSEMOVE = 0x200
WM_MOUSEWHEEL = 0x20a
WM_RBUTTONDOWN = 0x204
WM_RBUTTONUP = 0x205
WM_SETFOCUS = 7
WM_USER = 0x400
WA_ACTIVE = 1
WA_INACTIVE = 0

# Virtual-key codes -- the load-bearing half. keys.py builds vk_key_dict from these and
# post_message.py looks up every keypress in it, so a stub here would be posted to the
# game as a virtual-key code with no exception raised anywhere.
VK_BACK = 8
VK_CAPITAL = 0x14
VK_CONTROL = 0x11
VK_DELETE = 0x2e
VK_DOWN = 0x28
VK_END = 0x23
VK_ESCAPE = 0x1b
VK_F1 = 0x70
VK_F10 = 0x79
VK_F11 = 0x7a
VK_F12 = 0x7b
VK_F2 = 0x71
VK_F3 = 0x72
VK_F4 = 0x73
VK_F5 = 0x74
VK_F6 = 0x75
VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_HOME = 0x24
VK_INSERT = 0x2d
VK_LCONTROL = 0xa2
VK_LEFT = 0x25
VK_LMENU = 0xa4
VK_LSHIFT = 0xa0
VK_LWIN = 0x5b
VK_MENU = 0x12
VK_NEXT = 0x22
VK_NUMLOCK = 0x90
VK_PRIOR = 0x21
VK_RCONTROL = 0xa3
VK_RETURN = 13
VK_RIGHT = 0x27
VK_RMENU = 0xa5
VK_RSHIFT = 0xa1
VK_RWIN = 0x5c
VK_SCROLL = 0x91
VK_SHIFT = 0x10
VK_SNAPSHOT = 0x2c
VK_SPACE = 0x20
VK_TAB = 9
VK_UP = 0x26


def __getattr__(name):
    raise AttributeError(
        f"win32con.{name} is not in ok-script's Linux win32con subset. Upstream started "
        f"using a new constant; regenerate with `python3 tools/gen_win32con.py` after "
        f"adding it to GROUPS."
    )
