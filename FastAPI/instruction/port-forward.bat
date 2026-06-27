@echo off
title 2 - Network Bridge Configurator (Port 8001)

:: Hard check for Administrator rights
net session >nul 2>&1
if NOT %errorLevel% == 0 (
    echo [ERROR] You must right-click this file and select "Run as Administrator".
    pause
    exit /b
)

echo [1/3] Fetching Windows LAN IP (physical adapter only, skipping VPN)...
for /f "tokens=*" %%i in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' } | ForEach-Object { Get-NetIPAddress -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue } | Where-Object { $_.IPAddress -notlike '169.*' } | Select-Object -First 1 -ExpandProperty IPAddress"') do set WIN_IP=%%i

echo.
echo    LAN IP : %WIN_IP%
echo.

echo [2/3] Forcing IP Helper Service On...
sc config iphlpsvc start=auto >nul 2>&1
net start iphlpsvc >nul 2>&1

echo [3/3] Rebuilding Proxy using Localhost Bypass...
:: Wipe old rules
netsh interface portproxy delete v4tov4 listenport=8001 listenaddress=0.0.0.0 >nul 2>&1
netsh interface portproxy delete v4tov4 listenport=8001 listenaddress=%WIN_IP% >nul 2>&1

:: Bridge: Route LAN traffic to WSL localhost
netsh interface portproxy add v4tov4 listenport=8001 listenaddress=%WIN_IP% connectport=8001 connectaddress=127.0.0.1

:: Firewall rule
netsh advfirewall firewall delete rule name="Namecard Extract LAN Access" >nul 2>&1
netsh advfirewall firewall add rule name="Namecard Extract LAN Access" dir=in action=allow protocol=TCP localport=8001

echo.
echo ----------------------------------------------------
echo [SUCCESS] Network bridge complete!
echo Try accessing: http://%WIN_IP%:8001 from your phone.
echo ----------------------------------------------------
pause
