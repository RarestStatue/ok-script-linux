import sys as _sys  # underscore-prefixed: `ok/device/capture.py` re-exports this package with `import *`

from ok.device.capture_methods.adb import ADBCaptureMethod
from ok.device.capture_methods.base import BaseCaptureMethod, BaseWindowsCaptureMethod
from ok.device.capture_methods.bitblt import BitBltCaptureMethod, ForegroundBitBltCaptureMethod
from ok.device.capture_methods.bitblt_utils import (
    BGRA_CHANNEL_COUNT,
    PBYTE,
    PW_CLIENT_ONLY,
    PW_RENDERFULLCONTENT,
    BitBltCtxDummy,
    capture_by_bitblt,
    capture_desktop_by_bitblt,
    clean_up_bitblt,
    clean_up_desktop_bitblt,
    composite_hwnds,
    get_crop_point,
    parse_reg_flag,
    try_delete_dc,
)
from ok.device.capture_methods.browser import BrowserCaptureMethod, BrowserWGC, BrowserWindowAdapter
from ok.device.capture_methods.desktop_duplication import DesktopDuplicationCaptureMethod
from ok.device.capture_methods.hwnd_window import HwndWindow, check_pos, get_monitors_bounds, get_mute_state, is_window_in_screen_bounds, set_mute_state
from ok.device.capture_methods.image import ImageCaptureMethod
from ok.device.capture_methods.nemu_ipc import NemuIpcCaptureMethod
from ok.device.capture_methods.types import ColorChannel, ImageShape, decimal, is_digit, is_valid_hwnd
from ok.device.capture_methods.update import get_capture, get_win_graphics_capture, update_capture_method
from ok.device.capture_methods.windows_graphics import WindowsGraphicsCaptureMethod

# On Linux the Win32 window layer imports (the win32 stub sees to that) but cannot run, so
# shadow it with the X11 one. `HwndWindow` is rebound rather than renamed because that is
# the name `DeviceManager` imports at module level and constructs in `ensure_hwnd`;
# `ok/device/capture.py` re-exports this package with `import *` and the package declares no
# `__all__`, so the rebinding carries through. Line 21 above is the only place in the tree
# that imports those five helpers, so shadowing them here is complete.
if _sys.platform == 'linux':
    from ok.device.capture_methods.x11_window import (
        X11Window,
        check_pos,
        get_monitors_bounds,
        get_mute_state,
        is_window_in_screen_bounds,
        set_mute_state,
    )

    HwndWindow = X11Window
