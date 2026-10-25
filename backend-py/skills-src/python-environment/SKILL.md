---
name: python-environment
description: Which Python interpreter to use for any Bash-run Python script or command. Use this whenever a task needs to run Python.
---

# Python environment

If you need to run Python for anything, use Caroline's own isolated interpreter installed by
CarolineInstaller, not a system `python`/`python3` on PATH -- the system one may not exist, or
may belong to the user's own separate work (different version, different installed packages).

Path: `%LocalAppData%\Caroline\runtime\python\python.exe` (i.e.
`C:\Users\<user>\AppData\Local\Caroline\runtime\python\python.exe`).

If that path doesn't exist, this is a dev run where the installer never ran -- fall back to
whatever `python`/`python3` is actually on PATH.
