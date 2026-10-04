@echo off
rem guard: never forward -? / /? (PowerShell would run the script instead of showing help)
rem Wrapper: works from cmd.exe, PowerShell, Explorer or the Run dialog.
rem It bypasses the execution policy for THIS process only (no system change).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\vm-console.ps1" %*
