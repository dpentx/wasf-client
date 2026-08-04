@echo off

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo HATA: Bu scripti Yonetici olarak calistirman lazim.
    echo Sag tik -^> "Yonetici olarak calistir"
    pause
    exit /b 1
)

set DIR=%~dp0

echo == wfas_host.exe icin izin ekleniyor ==
netsh advfirewall firewall add rule name="WFAS Host - wfas_host (UDP)" dir=in action=allow protocol=UDP program="%DIR%wfas_host.exe" enable=yes profile=any
netsh advfirewall firewall add rule name="WFAS Host - wfas_host (TCP)" dir=in action=allow protocol=TCP program="%DIR%wfas_host.exe" enable=yes profile=any

echo == wfas_host_debug.exe icin izin ekleniyor ==
netsh advfirewall firewall add rule name="WFAS Host - wfas_host_debug (UDP)" dir=in action=allow protocol=UDP program="%DIR%wfas_host_debug.exe" enable=yes profile=any
netsh advfirewall firewall add rule name="WFAS Host - wfas_host_debug (TCP)" dir=in action=allow protocol=TCP program="%DIR%wfas_host_debug.exe" enable=yes profile=any

echo.
echo Tamam. Artik bu iki exe icin kalici izin var, her calistirmada
echo sormayacak.
echo.
pause
