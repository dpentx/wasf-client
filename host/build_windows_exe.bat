@echo off

echo == WFAS Host - tasinabilir exe derleniyor ==
echo.

python -m pip install --upgrade pip
python -m pip install pyinstaller pyaudiowpatch pystray pillow

echo.
echo -- konsolsuz (tray-only) surum --
python -m PyInstaller --onefile --windowed --name wfas_host wfas_host.py

echo.
echo -- konsollu (debug) surum --
python -m PyInstaller --onefile --console --name wfas_host_debug wfas_host.py

echo.
echo Bitti!
echo   dist\wfas_host.exe        -^> gunluk kullanim (konsolsuz, tray-only)
echo   dist\wfas_host_debug.exe  -^> sorun cikarsa bunu calistir, log gorunur
echo.
echo Ikisini de USB'ye kopyalayip okul bilgisayarinda calistirabilirsin.
echo   wfas_host.exe --key gizliAnahtar
echo.
echo Log dosyasi (windowed surumde bile tutulur): %%USERPROFILE%%\wfas_host.log
echo.
pause
