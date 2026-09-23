@echo off
setlocal
cd /d "%~dp0"
set "DATA_DIR=%USERPROFILE%\Downloads"
if not "%~1"=="" set "DATA_DIR=%~1"
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -X utf8 -m replenishment.importer --archives "%DATA_DIR%\IEK.zip" "%DATA_DIR%\Systeme electric.zip"
) else (
  py -3 -X utf8 -m replenishment.importer --archives "%DATA_DIR%\IEK.zip" "%DATA_DIR%\Systeme electric.zip"
)
if errorlevel 1 (
  echo Import failed. See the message above and README.md.
) else (
  echo Data imported. Restart StockPilot to load the new dataset.
)
pause
