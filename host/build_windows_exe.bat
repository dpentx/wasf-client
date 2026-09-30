@echo off

echo == WFAS Host - tasinabilir exe derleniyor ==
echo.

python -m pip install --upgrade pip
python -m pip install -r requirements-windows.txt

echo.
echo -- konsolsuz (tray-only) surum --
python -m PyInstaller --onefile --windowed --name wfas_host --collect-data sv_ttk --hidden-import pystray._win32 wfas_host.py

echo.
echo -- konsollu (debug) surum --
python -m PyInstaller --onefile --console --name wfas_host_debug --collect-data sv_ttk --hidden-import pystray._win32 wfas_host.py

echo.
echo Bitti!
echo   dist\wfas_host.exe        -^> gunluk kullanim (konsolsuz, tray-only)
echo   dist\wfas_host_debug.exe  -^> sorun cikarsa bunu calistir, log gorunur
echo.
echo Ikisini de USB'ye kopyalayip okul bilgisayarinda calistirabilirsin.
echo   wfas_host.exe --key gizliAnahtar
echo.
echo Host'un kendi hoparlorunden ses cikmasin istersen: Ayarlar ^> Sistem ^> Ses'ten
echo kullanilmayan bir cikisi (or. bos kulaklik jack'i) elle varsayilan yap.
echo.
echo Log dosyasi (windowed surumde bile tutulur): %USERPROFILE%\wfas_host.log
echo.
echo Not: GitHub Actions da ayni exe'leri otomatik derler (Actions ^> Build host).
echo.
pause
