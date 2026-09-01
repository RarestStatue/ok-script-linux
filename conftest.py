"""Install the Linux Win32 compatibility shim before pytest collects anything.

`ok.compat.win32_stub.install()` has to run before the first `import ok.<anything win32>`,
and pytest imports test modules -- which import `ok` -- during collection. A root conftest
is the earliest hook that is guaranteed to run first. No-op on Windows.
"""

from ok.compat.win32_stub import install

install()
