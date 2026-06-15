@echo off
chcp 65001 >nul
cd /d "%~dp0"

title a-transaction2_claude

:: 启动后台进程持续刷新标题
start /b "" powershell -Command "$title='a-transaction2_claude'; while(1){ $Host.UI.RawUI.WindowTitle=$title; Start-Sleep -Milliseconds 300 }"

:: 记录后台进程的 PID（可选，方便结束后精确杀死）
for /f "tokens=2" %%a in ('tasklist /fi "imagename eq powershell.exe" /nh') do set bgpid=%%a

:: 运行主程序
claude --dangerously-skip-permissions -c

:: 杀掉后台 PowerShell 进程
if defined bgpid taskkill /pid %bgpid% /f >nul 2>&1
:: 最终再确保一下标题
title a-transaction2_claude
pause