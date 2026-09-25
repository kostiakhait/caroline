@echo off
setlocal EnableDelayedExpansion

:: -- Caroline Companion (Android) release build ------------------------------
::
:: Output: app\build\outputs\apk\release\app-release.apk (signed) and
:: out\version.txt (the version string deploy.bat publishes to the storefront).
::
:: Signing: d:\REPO\tf38key.jks (alias tf38key) -- the same key Ratatosk and
:: ShortNerdCat use. Pass SNC_SIGN_PASSWORD as an env var, --password, or the
:: script prompts. Override the keystore path with SNC_KEYSTORE.

set OUTDIR=%~dp0out

:parse_args
if not "%~1"=="--password" goto parse_end
set SNC_SIGN_PASSWORD=%~2
shift
shift
goto parse_args
:parse_end
if not "%~1"=="" (echo Unknown option: %~1 & exit /b 1)

if "!APP_VERSION!" neq "" (
    set VERSION=!APP_VERSION!
) else (
    for /f "tokens=*" %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMddHHmm"') do set VERSION=%%i
    if "!VERSION!"=="" set VERSION=dev
)
:: Monotonic versionCode: minutes since the Unix epoch (fits an Int until ~6000 AD).
for /f "tokens=*" %%i in ('powershell -NoProfile -Command "[int]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() / 60)"') do set VERSION_CODE=%%i
if "!VERSION_CODE!"=="" (echo ERROR: could not compute versionCode. & exit /b 1)

if "!SNC_SIGN_PASSWORD!"=="" (
    set /p SNC_SIGN_PASSWORD="Keystore password (d:\REPO\tf38key.jks): "
)
if "!SNC_SIGN_PASSWORD!"=="" (echo ERROR: signing password is required. & exit /b 1)

echo.
echo === Caroline Companion Android Build ===
echo Version : !VERSION! (versionCode !VERSION_CODE!)
echo.

if not exist "%OUTDIR%" mkdir "%OUTDIR%"
echo !VERSION!> "%OUTDIR%\version.txt"

call "%~dp0gradlew.bat" assembleRelease --console=plain -PappVersion=!VERSION! -PappVersionCode=!VERSION_CODE!
if errorlevel 1 (echo ERROR: gradle build failed. & exit /b 1)

echo.
echo OK: %~dp0app\build\outputs\apk\release\app-release.apk
endlocal
