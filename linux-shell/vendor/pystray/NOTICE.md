Vendored package: pystray 0.19.4
Source: https://pypi.org/project/pystray/0.19.4/
Copyright (C) 2016-2022 Moses Palmér
License: GNU Lesser General Public License v3 (see COPYING, COPYING.LGPL)

Why vendored instead of `pip install`-ed: `linux-shell/caroline_shell.py`
runs under the Linux system python3 (needed for the `python3-gi`/WebKit2
GObject-Introspection bindings, which are system packages, not pip-
installable), and PEP 668's externally-managed-environment marker blocks
`pip install` into that interpreter on current Debian/Ubuntu -- the same
reason `python3-xlib` is an apt package rather than a pip one (see
docs/LINUX_PORT_PLAN.md). pystray itself is not a standard Debian/Ubuntu
apt package, so it is vendored here as plain source instead (it is pure
Python, no compiled extensions, MIT-compatible-at-the-source-level to
redistribute this way under LGPLv3 as long as this directory -- the
library's own unmodified code plus its license files -- stays separate
and replaceable, which it does: nothing in Caroline's own code is copied
into it, and it can be swapped for a newer pystray release by replacing
this directory alone).

pystray's own runtime dependencies (Pillow, for loading the tray icon
image, and six) are NOT vendored -- both have their own standard Debian/
Ubuntu apt packages (`python3-pil`, `python3-six`), so they install the
same way `python3-gi`/`python3-xlib` already do, with no PEP 668 conflict
and no license-vendoring question of their own.

Not modified from the upstream release. To update: download a newer
sdist (`pip download pystray==<version> --no-deps --no-binary :all:` on
any machine, unpack it, replace this directory's .py files and COPYING*
files), update the version above, and re-check this file's own reasoning
still holds.
