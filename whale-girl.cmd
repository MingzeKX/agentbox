@echo off
rem ---------------------------------------------------------------------------
rem agentbox launcher wrapper.
rem
rem Windows ships with ExecutionPolicy=Restricted, which refuses to run any
rem unsigned .ps1 ("未对文件进行数字签名").  This wrapper starts PowerShell with
rem -ExecutionPolicy Bypass for THIS process only, so nothing about the machine's
rem policy needs to change (no admin rights, no set-executionpolicy).
rem
rem Usage (same arguments as whale-girl.ps1):
rem   whale-girl.cmd                 show usage
rem   whale-girl.cmd -Voice          voice mode
rem   whale-girl.cmd -Message "hi"   one non-interactive turn
rem ---------------------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0whale-girl.ps1" %*
