"""Platform-neutral geometry helpers, split out of `bitblt_utils`.

These two functions are pure arithmetic/string parsing, but they lived in a module that
opens with `import win32con, win32gui, win32ui`. The Linux capture path needs
`get_crop_point` and must not drag the BitBlt machinery in to get it, so they live here and
`bitblt_utils` re-exports them for Windows callers.

`get_crop_point`'s asymmetry is deliberate and load-bearing -- `x` is the horizontal border
(rounded, split evenly) while `y` is the *title bar*, i.e. all remaining vertical slack
after subtracting one border. Do not "fix" it into `(frame_height - target_height) / 2`.
"""


def get_crop_point(frame_width, frame_height, target_width, target_height):
    x = round((frame_width - target_width) / 2)
    y = (frame_height - target_height) - x
    return x, y


def parse_reg_flag(value, flag_name):
    if not value or not isinstance(value, str): return None
    parts = value.split(';')
    for part in parts:
        kv = part.split('=')
        if len(kv) == 2 and kv[0].strip() == flag_name:
            try:
                v = int(kv[1])
                return v % 2 != 0
            except:
                pass
    return None
