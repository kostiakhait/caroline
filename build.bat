@echo off
:: Thin wrapper -- the real logic lives in Makefile's `build` target now.
set SCRIPT_DIR=%~dp0
set MAKE=make
where make >nul 2>nul || set MAKE="C:\Program Files (x86)\GnuWin32\bin\make.exe"
cd /d "%SCRIPT_DIR%" && %MAKE% build
