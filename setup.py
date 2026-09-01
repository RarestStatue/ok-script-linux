import os

import setuptools

os.environ["PYTHONIOENCODING"] = "utf-8"

# The upstream release this fork is based on, plus a PEP 440 local segment. `>=2.0.5`
# pins are satisfied by `2.0.5+linux.N`, and the local segment keeps a fork build from
# ever being mistaken for the published wheel. Bump the `.N` on every rebase onto a new
# upstream tag; change the base to match the tag itself.
FORK_VERSION = "2.0.5+linux.1"

# Upstream derived the version by asking PyPI for ok-script's latest release and adding
# one. That is non-hermetic (it needs the network and `get_pypi_latest_version`, which is
# not in build-system.requires) and meaningless for a fork, so it is gone. CI can still
# override explicitly.
VERSION_NUM = os.environ.get("OK_SCRIPT_BUILD_VERSION") or FORK_VERSION

setuptools.setup(version=VERSION_NUM)
