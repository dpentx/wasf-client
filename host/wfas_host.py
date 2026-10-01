#!/usr/bin/env python3
"""
WFAS taşınabilir host (server).

Sistem sesini yakalayıp wfas_native_client.py'nin konuştuğu WFAS UDP
protokolüyle (discovery beacon + unicast handshake + ham 16-bit PCM akışı)
ağa yayınlar. Kurulum gerektirmez:

  - Linux: PulseAudio/PipeWire'ın `parec` aracıyla (genelde zaten kurulu).
  - Windows: `pyaudiowpatch` pip paketiyle (WASAPI loopback, sürücü kurulumu
    YOK). İstersen PyInstaller ile tek dosyalık taşınabilir .exe'ye
    paketleyip USB'den okul bilgisayarında çalıştırabilirsin - bkz.
    build_windows_exe.bat.

Desteklenenler: Off ve Key güvenlik modları, tek unicast client (Multicast/
Ask modu yok - istemcimiz zaten sadece Unicast konuşuyor). Bölüm 8
(ChaCha20-Poly1305 şifreleme) burada da yok; Key modu sadece kimlik
doğrulama sağlar, trafiği şifrelemez.

Kullanım (Linux):
    python3 wfas_host.py
    python3 wfas_host.py --key gizliAnahtar

Kullanım (Windows, taşınabilir python ile):
    python.exe wfas_host.py --key gizliAnahtar
"""
import argparse
import array
import hashlib
import hmac
import json
import os
import platform
import queue
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

DISCOVERY_GROUP = "239.255.0.1"
DISCOVERY_PORT = 9091
DISCOVERY_PREFIX = "WIFI_AUDIO_STREAMER_DISCOVERY"
PROTOCOL_VERSION = 2
BEACON_INTERVAL = 1.0
CHUNK_MS = 20

IS_WINDOWS = platform.system() == "Windows"

LOG_PATH = os.path.join(os.path.expanduser("~"), "wfas_host.log")

def log(msg):
    """Konsola (varsa) VE ~/wfas_host.log dosyasına yazar. --windowed
    (konsolsuz) derlemelerde konsol yoktur ama log dosyası hep tutulur,
    böylece tray-only modda da sorun çıkarsa geriye dönük bakılabilir."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def hmac_hex(key: bytes, msg: str) -> str:
    return hmac.new(key, msg.encode("ascii"), hashlib.sha256).hexdigest()

def parse_kv(msg, key):
    for tok in msg.split(";"):
        if tok.startswith(key + "="):
            return tok.split("=", 1)[1]
    return None

class LinuxVirtualSink:
    """PulseAudio/PipeWire üzerinde 'WFAS Sanal Hoparlör' adında bir
    null-sink oluşturup varsayılan çıkış yapar. Bunun sonucunda:

      - Sistem sesi fiziksel hoparlörden/kulaklıktan ÇIKMAZ (null-sink
        hiçbir donanıma bağlı değil, sesi yutar).
      - Sistem ses düzeyi tuşları / ses karıştırıcısı artık bu sanal
        cihazın seviyesini kontrol eder - ve LinuxCapture zaten
        "varsayılan sink'in monitor'ü"nü okuduğu için, sistem sesini
        açıp kısmak doğrudan client'a giden akışın seviyesini değiştirir.

    Host kapanırken (disable) önceki varsayılan çıkış otomatik geri
    yüklenir, host çökse bile bir sonraki başlangıçta artık kalan eski
    modül varsa temizlenir."""

    SINK_NAME = "WFAS_Virtual_Speaker"
    SINK_DESC = "WFAS_Sanal_Hoparlor"

    def __init__(self):
        self._module_id = None
        self._previous_default = None
        self.enabled = False

    def _pactl(self, *args):
        return subprocess.run(["pactl", *args], capture_output=True,
                               encoding="utf-8", errors="ignore", timeout=3)

    def is_available(self):
        return subprocess.run(["which", "pactl"], capture_output=True).returncode == 0

    def _cleanup_stale(self):
        """Önceki bir çalıştırmadan (örn. kill -9 sonrası) kalmış
        WFAS null-sink modülü varsa temizler."""
        out = self._pactl("list", "short", "modules").stdout
        for line in out.splitlines():
            if "module-null-sink" in line and f"sink_name={self.SINK_NAME}" in line:
                mod_id = line.split("\t")[0]
                self._pactl("unload-module", mod_id)

    def enable(self):
        if not self.is_available():
            raise RuntimeError(
                "`pactl` bulunamadı, sanal hoparlör için PulseAudio/PipeWire gerekli."
            )
        self._cleanup_stale()

        prev = self._pactl("get-default-sink").stdout.strip()
        self._previous_default = prev or None

        result = self._pactl(
            "load-module", "module-null-sink",
            f"sink_name={self.SINK_NAME}",
            f"sink_properties=device.description={self.SINK_DESC}",
        )
        module_id = result.stdout.strip()
        if not module_id.isdigit():
            raise RuntimeError(f"module-null-sink yüklenemedi: {result.stderr.strip()}")
        self._module_id = module_id

        self._pactl("set-default-sink", self.SINK_NAME)
        self.enabled = True
        log(f"[bilgi] Sanal hoparlör aktif ({self.SINK_NAME}). "
            f"Host cihazdan artık ses çıkmayacak, sistem ses düzeyi client "
            f"akışını kontrol edecek. (Önceki çıkış: {prev or 'bilinmiyor'})")

    def disable(self):
        if not self.enabled:
            return
        if self._previous_default:
            self._pactl("set-default-sink", self._previous_default)
        if self._module_id:
            self._pactl("unload-module", self._module_id)
        self.enabled = False
        log("[bilgi] Sanal hoparlör kapatıldı, önceki ses çıkışı geri yüklendi.")

class LinuxCapture:
    """PulseAudio/PipeWire üzerinden varsayılan çıkışın monitor kaynağını
    `parec` ile ham PCM olarak okur."""

    def __init__(self, rate=48000, channels=2):
        self.rate = rate
        self.channels = channels
        self.proc = None

    def _monitor_source(self):
        try:
            sink = subprocess.check_output(
                ["pactl", "get-default-sink"], encoding='utf-8', errors='ignore', timeout=3
            ).strip()
            return f"{sink}.monitor"
        except Exception:
            return None

    def start(self):
        if subprocess.run(["which", "parec"], capture_output=True).returncode != 0:
            raise RuntimeError(
                "`parec` bulunamadı. Kurulum: sudo apt-get install pulseaudio-utils "
                "(PipeWire kullanıyorsan pipewire-pulse genelde zaten sağlıyor)"
            )
        source = self._monitor_source()
        cmd = ["parec", "--format=s16le", f"--rate={self.rate}",
               f"--channels={self.channels}", "--latency-msec=20"]
        if source:
            cmd += ["-d", source]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    def read(self, nbytes):
        data = self.proc.stdout.read(nbytes)
        return data or None

    def stop(self):
        if self.proc:
            self.proc.terminate()

# WASAPI loopback, varsayilan cikisin karisim formatini (mix format) verir.
# Bazi laptoplarda bu 6/8 kanal (HDMI/ekran, Dolby/Sonic, 5.1-7.1 ayari) ya da
# 96/192 kHz olabiliyor. 20 ms'lik chunk o zaman 4096 baytin (client'in
# recvfrom tamponu) cok uzerine cikiyor, paketler kesiliyor ve client sessiz /
# cizirtili kaliyordu. Bu yuzden coklu kanal host'ta stereoya indirilir.
_K = 0.7071
_DOWNMIX = {
    4: ([1, 0, _K, 0], [0, 1, 0, _K]),                                 # FL FR BL BR
    5: ([1, 0, _K, _K, 0], [0, 1, _K, 0, _K]),                         # FL FR FC BL BR
    6: ([1, 0, _K, 0, _K, 0], [0, 1, _K, 0, 0, _K]),                   # 5.1
    8: ([1, 0, _K, 0, _K, 0, _K, 0], [0, 1, _K, 0, 0, _K, 0, _K]),     # 7.1
}

def _mix_channel(chs, weights, gain):
    idx = [(i, w) for i, w in enumerate(weights) if w]
    cols = [chs[i] for i, _ in idx]
    ws = [w for _, w in idx]
    out = []
    for frame in zip(*cols):
        v = int(gain * sum(f * w for f, w in zip(frame, ws)))
        out.append(32767 if v > 32767 else -32768 if v < -32768 else v)
    return array.array("h", out)

def downmix_to_stereo(data, n):
    """n kanalli 16-bit little-endian PCM'i stereoya indirir (n<=2 ise dokunmaz)."""
    if n <= 2:
        return data
    a = array.array("h")
    a.frombytes(data[: len(data) - (len(data) % (2 * n))])
    frames = len(a) // n
    chs = [a[i::n] for i in range(n)]
    wl, wr = _DOWNMIX.get(n, ([1] + [0] * (n - 1), [0, 1] + [0] * (n - 2)))
    gain = 0.6 if n in _DOWNMIX and n >= 5 else 1.0
    out = array.array("h", bytes(4 * frames))
    out[0::2] = _mix_channel(chs, wl, gain)
    out[1::2] = _mix_channel(chs, wr, gain)
    return out.tobytes()

class WindowsCapture:
    """WASAPI loopback ile sistem sesini (varsayılan hoparlör çıkışı) yakalar.
    Sanal ses kartı/sürücü kurulumu GEREKMEZ - sadece `pyaudiowpatch` pip
    paketi (PortAudio'nun WASAPI-loopback yamalı hâli)."""

    def __init__(self, rate=48000, channels=2):
        self.rate = rate
        self.channels = channels
        self._pa = None
        self._stream = None
        self._buf = queue.Queue()

    def start(self):
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            raise RuntimeError(
                "pyaudiowpatch kurulu değil. Kurulum: python.exe -m pip install pyaudiowpatch"
            )
        self._pyaudio = pyaudio
        self._pa = pyaudio.PyAudio()
        wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = self._pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        if not default_speakers.get("isLoopbackDevice"):
            for loopback in self._pa.get_loopback_device_info_generator():
                if default_speakers["name"] in loopback["name"]:
                    default_speakers = loopback
                    break
            else:
                raise RuntimeError("WASAPI loopback cihazı bulunamadı")

        self.rate = int(default_speakers["defaultSampleRate"])
        self.channels = int(default_speakers["maxInputChannels"])
        self._src_channels = self.channels
        # client'a giden akis en fazla stereo: coklu kanal read()'de indirilir
        self.channels = 2 if self._src_channels > 2 else self._src_channels
        log(f"[capture] cihaz: {default_speakers['name']} | {self.rate} Hz | "
            f"{self._src_channels} kanal -> client'a {self.channels} kanal")

        def _cb(in_data, frame_count, time_info, status):
            self._buf.put(in_data)
            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self._src_channels,
            rate=self.rate,
            input=True,
            input_device_index=default_speakers["index"],
            frames_per_buffer=int(self.rate * CHUNK_MS / 1000),
            stream_callback=_cb,
        )
        self._stream.start_stream()

    def read(self, nbytes):
        try:
            data = self._buf.get(timeout=1.0)
        except queue.Empty:
            return None
        return downmix_to_stereo(data, self._src_channels)

    def stop(self):
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
        if self._pa:
            self._pa.terminate()

def make_capture(rate, channels):
    return WindowsCapture(rate, channels) if IS_WINDOWS else LinuxCapture(rate, channels)

# --- Windows: sanal hoparlör YOK ---
#
# VB-CABLE denendi ve kaldırıldı: VB-Audio'nun kendi geliştiricisi VB-CABLE'ın
# gerçek bir kazanç (gain) kontrolü uygulamadığını doğruluyor - Windows ses
# panelindeki/görev çubuğundaki kaydırıcı ve klavye tuşları CABLE Input için
# sadece görsel, sese hiçbir etkisi yok. Bu da tam olarak bizim ihtiyacımızla
# (host sessiz + Windows panelinden client sesini kısıp açabilme) çelişiyor,
# bu yüzden bu yoldan vazgeçildi.
#
# Windows'ta host'u sessize almanın önerilen yolu artık DONANIMSAL: host
# makinede kullanılmayan bir ses çıkışı (ör. boş kulaklık jack'ine takılı,
# hoparlörü olmayan/kırık bir kulaklık) varsayılan çıkış yapılır. Gerçek bir
# donanım endpoint'i olduğu için Windows'un master volume'u onu normal
# şekilde etkiler (loopback capture da bunu yansıtır), ama fiziksel olarak
# hiçbir yerden duyulabilir ses çıkmaz. Bu, host'a herhangi bir sürücü
# kurulmasını gerektirmez; sadece Ayarlar > Sistem > Ses'ten elle o çıkışı
# varsayılan seçmen yeterli - `--no-virtual-speaker` ile başlat, kod
# Windows'ta ayrıca bir şey yapmaya çalışmaz, o an sistemde varsayılan olan
# çıkışı (fiziksel ya da yukarıdaki gibi "sessiz" bir donanım çıkışı) kullanır.

class GainState:
    def __init__(self, value=1.0):
        self._lock = threading.Lock()
        self._value = value

    def get(self):
        with self._lock:
            return self._value

    def set(self, value):
        with self._lock:
            self._value = value

def apply_gain(data: bytes, gain: float) -> bytes:
    """16-bit little-endian PCM `data`'ya `gain` çarpanını uygular, taşmaları
    (clipping) -32768..32767 aralığına kırpar. gain==1.0 iken hiç dokunmaz."""
    if gain == 1.0:
        return data
    samples = array.array("h")
    samples.frombytes(data)
    if gain <= 0.0:
        for i in range(len(samples)):
            samples[i] = 0
    else:
        for i in range(len(samples)):
            v = int(samples[i] * gain)
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            samples[i] = v
    return samples.tobytes()

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
# netsh cikti kodlamasi Windows'ta OEM kod sayfasidir (Turkce'de cp857); utf-8
# ile okuyunca yoldaki ü/ş/ı/ğ gibi harfler kayboluyor ve kural yolu asla
# eslesmiyordu.
_NETSH_ENC = "oem" if IS_WINDOWS else "utf-8"

def _this_exe_path():
    """Windows Firewall kuralinin 'program=' degeri icin GERCEKTEN calisan
    yurutulebilir dosyanin yolu. Windows Firewall izni process'in kendisine
    (binary'ye) gore verir, .py script dosyasina gore DEGIL - program=
    olarak bir .py yolu yazmak sessizce hicbir zaman eslesmiyor ve inbound
    engellenmeye devam ediyordu. sys.executable hem PyInstaller ile
    derlenmis .exe'de (frozen) hem de dogrudan 'python.exe script.py' ile
    calistirilan durumda dogru binary'yi (sirasiyla .exe'nin kendisi /
    calistiran python.exe) verir."""
    return os.path.abspath(sys.executable)

def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def _firewall_rule_program(name):
    """Var olan bir kuralin `program=` degerini dondurur (kural yoksa None).
    Sadece isim eslesmesi yeterli degil: eski bir build'den kalma, artik
    gecersiz bir .exe yoluna bagli bir kural da isimce 'var' gorunur ama
    gercekte hicbir seyi izinlemiyordur - bu yuzden yolu da karsilastirmak
    gerekiyor. Etiket dili (Program:/Programm: ...) yerel ayara gore
    degistiginden etikete degil, degerin 'C:\\...' gibi bir yol olmasina
    bakilir."""
    import re
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}", "verbose"],
            capture_output=True, encoding=_NETSH_ENC, errors='ignore', timeout=8, creationflags=_NO_WINDOW,
        )
        if r.returncode != 0 or name not in r.stdout:
            return None
        for line in r.stdout.splitlines():
            if ":" not in line:
                continue
            value = line.split(":", 1)[1].strip()
            if re.match(r"^[A-Za-z]:\\", value):
                return value
        return None
    except Exception:
        return None

def _firewall_rule_matches(name, exe_path):
    """Kural var VE gercekten su anki exe'ye mi bagli, onu kontrol eder."""
    prog = _firewall_rule_program(name)
    return bool(prog) and os.path.normcase(os.path.abspath(prog)) == os.path.normcase(os.path.abspath(exe_path))

def _delete_firewall_rule(name):
    try:
        subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"],
                        capture_output=True, encoding='utf-8', errors='ignore', timeout=10, creationflags=_NO_WINDOW)
    except Exception:
        pass

def _run_elevated_and_wait(file_path, params, timeout_ms=20000):
    """`file_path`'i (ör. bir .bat) 'runas' fiiliyle çalıştırır - tek bir
    UAC penceresi çıkar, kullanıcı onaylarsa işlem bitene kadar bekler.
    Kullanıcı reddederse ya da bir hata olursa sessizce False döner."""
    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hKeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.hwnd = None
    sei.lpVerb = "runas"
    sei.lpFile = file_path
    sei.lpParameters = params
    sei.lpDirectory = os.path.dirname(file_path) or None
    sei.nShow = SW_HIDE

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        return False
    if sei.hProcess:
        ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, timeout_ms)
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return True

DEFAULT_HOTSPOT_SSID = "wfas wifi"
DEFAULT_HOTSPOT_PASSWORD = "wfasaudio1"

def _ps_quote(s):
    """PowerShell tek tirnakli string icin kacis (tek tirnagi ikiye katla)."""
    return "'" + str(s).replace("'", "''") + "'"

def ensure_mobile_hotspot(ssid, password, quiet_if_ok=False):
    """Mobil Hotspot'u acmaya calisir. Basariliysa True, degilse False doner
    (log zaten burada basiliyor). quiet_if_ok=True iken basariliysa log
    basmaz - watchdog dongusunde her turda ayni 'etkin' mesajini tekrar
    tekrar yazmamak icin."""
    if not IS_WINDOWS:
        return True
    ps_ssid = _ps_quote(ssid)
    ps_pass = _ps_quote(password)
    ps_script = f"""
$ErrorActionPreference = "Stop"
try {{
    Add-Type -AssemblyName System.Runtime.WindowsRuntime

    # WinRT'nin IAsyncAction/IAsyncOperation<T> donuslerini PowerShell'e ham
    # System.__ComObject olarak gelir; bunlarin GetResults() metodu generic
    # oldugu icin PowerShell'in dogrudan metot cozumlemesiyle bulunamaz
    # ("does not contain a method named 'GetResults'"). Cozum: WinRT
    # SystemExtensions'daki AsTask() uzanti metodunu reflection ile ilgili
    # generic tipe gore olusturup gercek bir .NET Task'a cevirmek.
    $asTaskGenericOp = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    }})[0]
    $asTaskAction = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
    }})[0]

    function AwaitAction($WinRtAction) {{
        $t = $asTaskAction.Invoke($null, @($WinRtAction))
        $t.Wait(-1) | Out-Null
    }}
    function AwaitOperation($WinRtOp, $ResultType) {{
        $t = $asTaskGenericOp.MakeGenericMethod($ResultType).Invoke($null, @($WinRtOp))
        $t.Wait(-1) | Out-Null
        return $t.Result
    }}

    [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime] | Out-Null
    $profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
    if (-not $profile) {{ Write-Output "ERR:aktif internet baglantisi bulunamadi"; exit 1 }}

    [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] | Out-Null
    $manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)

    $config = $manager.GetCurrentAccessPointConfiguration()
    $alreadyOn = ($manager.TetheringOperationalState -eq [Windows.Networking.NetworkOperators.TetheringOperationalState]::On)
    $sameConfig = ($config.Ssid -eq {ps_ssid} -and $config.Passphrase -eq {ps_pass})

    if ($alreadyOn -and $sameConfig) {{
        # Zaten dogru ayarla acik - burada YENIDEN yapilandirmak/baslatmak
        # WiFi radyosunu kisa sureligine resetleyip bagli client'i agdan
        # dusurur (watchdog'un 45sn'de bir bunu tekrar tekrar tetiklemesi
        # tam da bu sorunun sebebiydi). Hicbir seye dokunmadan cik.
        Write-Output "OK"
        exit 0
    }}

    if (-not $sameConfig) {{
        $config.Ssid = {ps_ssid}
        $config.Passphrase = {ps_pass}
        AwaitAction ($manager.ConfigureAccessPointAsync($config))
    }}

    if (-not $alreadyOn -or -not $sameConfig) {{
        $result = AwaitOperation ($manager.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
        if ($result.Status -ne [Windows.Networking.NetworkOperators.TetheringOperationStatus]::Success) {{
            Write-Output ("ERR:baslatma basarisiz - " + $result.Status)
            exit 1
        }}
    }}
    Write-Output "OK"
}} catch {{
    Write-Output ("ERR:" + $_.Exception.Message)
}}
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True, encoding='utf-8', errors='ignore', timeout=30, creationflags=_NO_WINDOW,
        )
        out = (r.stdout or "").strip().splitlines()
        last = out[-1] if out else ""
        if last == "OK":
            if not quiet_if_ok:
                log(f"[hotspot] '{ssid}' Mobil Hotspot etkin - client bu agi arayip otomatik baglanabilir.")
            return True
        reason = last or (r.stderr or "").strip() or "bilinmeyen hata"
        log(f"[hotspot] Mobil Hotspot otomatik acilamadi ({reason}). "
            "Elle Ayarlar -> Ag ve Internet -> Mobil Hotspot'tan acabilir, "
            "ya da mevcut bir AG'a baglanip client'i --host ile yonlendirebilirsin.")
        return False
    except Exception as e:
        log(f"[hotspot] Mobil Hotspot denemesi basarisiz: {e}")
        return False

def hotspot_watchdog(ssid, password, stop_event, check_interval=45):
    """Windows, hicbir cihaz baglanmayinca Mobil Hotspot'u birkac dakika
    icinde kendiliginden kapatabiliyor (guc tasarrufu ozelligi). Bu thread
    periyodik olarak durumu kontrol edip gerekirse sessizce yeniden acar,
    boylece host uygulamasini yeniden baslatmaya gerek kalmaz. En kalici
    cozum Windows'ta Ayarlar -> Mobil Hotspot -> 'cihaz baglanmadiginda
    otomatik kapat' secenegini kapatmak - bu thread ona ek bir guvenlik
    agi."""
    if not IS_WINDOWS:
        return
    was_on = True
    while not stop_event.wait(check_interval):
        ok = ensure_mobile_hotspot(ssid, password, quiet_if_ok=True)
        if ok and not was_on:
            log(f"[hotspot] '{ssid}' tekrar etkinlestirildi (Windows kapatmis olabilirdi).")
        was_on = ok

def _firewall_ps_script(rule_udp, rule_tcp, exe_path, base):
    """Kurallari temizleyip yeniden ekleyen PowerShell betigi.

    Onceki surumde gecici bir .bat yazilip 'mbcs' ile kodlaniyordu; cmd ise
    .bat'i OEM kod sayfasiyla okur. Yolda Turkce karakter varsa (Masaüstü,
    Kullanıcı adı vb.) kural bozuk bir yola baglaniyor ve Windows engellemeye
    devam ediyordu. Burada betik -EncodedCommand (UTF-16) ile verildigi icin
    yol birebir korunur.

    Ek olarak Windows'un ilk calistirmada KENDILIGINDEN actigi kurallari
    ('wfas_host.exe' adli, cogu zaman ENGELLE): 'Iptal'e basilinca, standart
    kullanici olunca ya da pencere hic gelmeyince olusur. Engelleme kurali
    izin kuralindan onceliklidir ve varken Windows bir daha soru sormaz, bu
    yuzden bunlari da siliyoruz."""
    q = _ps_quote
    return (
        "$ErrorActionPreference='SilentlyContinue'\n"
        f"$exe={q(exe_path)}\n"
        f"foreach($n in @({q(rule_udp)},{q(rule_tcp)})){{ Remove-NetFirewallRule -DisplayName $n }}\n"
        "Get-NetFirewallApplicationFilter -Program $exe | Get-NetFirewallRule | "
        "Remove-NetFirewallRule\n"
        f"New-NetFirewallRule -DisplayName {q(rule_udp)} -Direction Inbound -Action Allow "
        "-Protocol UDP -Program $exe -Profile Any | Out-Null\n"
        f"New-NetFirewallRule -DisplayName {q(rule_tcp)} -Direction Inbound -Action Allow "
        "-Protocol TCP -Program $exe -Profile Any | Out-Null\n"
    )

def _ps_encoded_args(script):
    import base64
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-EncodedCommand", enc]

def ensure_firewall_rule():
    """wfas_host'un kendi .exe/.py yolu için inbound UDP+TCP izni yoksa
    ekler. Portable/USB kullanım için: ayrı bir .bat'ı elle 'Yönetici
    olarak çalıştır' etmeye gerek bırakmaz. Sadece Windows'ta çalışır ve
    hata durumunda (UAC reddi, netsh yoksa vs.) sessizce devam eder -
    audio yayınını asla engellemez."""
    if not IS_WINDOWS:
        return

    exe_path = _this_exe_path()
    base = os.path.splitext(os.path.basename(exe_path))[0]
    rule_udp = f"WFAS Host - {base} (UDP)"
    rule_tcp = f"WFAS Host - {base} (TCP)"

    ours_ok = (_firewall_rule_matches(rule_udp, exe_path)
               and _firewall_rule_matches(rule_tcp, exe_path))
    # Windows'un kendi actigi 'wfas_host.exe' kurallari varsa (engelle olabilir)
    # bizim kurallarimiz dogru olsa bile temizlenmeli.
    # Sadece derlenmis exe icin ve sadece program yolu BIZIM exe ile ayniysa
    # (python.exe ile calisirken baska programlarin kurallarina dokunma).
    auto_prog = _firewall_rule_program(base + ".exe") if getattr(sys, "frozen", False) else None
    auto_rules = bool(auto_prog) and (os.path.normcase(os.path.abspath(auto_prog))
                                      == os.path.normcase(os.path.abspath(exe_path)))
    if ours_ok and not auto_rules:
        return

    log(f"[firewall] '{base}' icin gelen (inbound) izin eksik/eski"
        f"{' (Windows kendi kuralini olusturmus)' if auto_rules else ''}, ekleniyor... "
        f"yol: {exe_path}")

    try:
        args = _ps_encoded_args(_firewall_ps_script(rule_udp, rule_tcp, exe_path, base))
        if _is_admin():
            r = subprocess.run(["powershell"] + args, capture_output=True,
                               encoding=_NETSH_ENC, errors='ignore', timeout=30,
                               creationflags=_NO_WINDOW)
            if r.returncode != 0:
                log(f"[firewall] PowerShell hata kodu {r.returncode}: {(r.stderr or '').strip()[:300]}")
            ok = True
        else:
            log("[firewall] yonetici degilim, UAC penceresi isteniyor "
                "(gorev cubugunda yanip sonuyorsa ona tikla)...")
            ok = _run_elevated_and_wait("powershell.exe", subprocess.list2cmdline(args),
                                        timeout_ms=30000)
        if ok and _firewall_rule_matches(rule_udp, exe_path) and _firewall_rule_matches(rule_tcp, exe_path):
            log("[firewall] izin eklendi.")
        elif not ok:
            log("[firewall] UAC penceresi acilamadi ya da reddedildi. "
                "Okul/yonetilen bir bilgisayarsa IT politikasi admin haklarini "
                "kapatmis olabilir - bu durumda add_firewall_rules.bat'i yonetici "
                "olarak calistirman ya da BT'den izin istemen gerekir.")
        else:
            log("[firewall] komut calisti ama kural dogrulanamadi - "
                "Windows Guvenlik Duvari > Gelismis Ayarlar > Gelen Kurallar'da "
                f"'{rule_udp}' var mi ve program yolu '{exe_path}' mi kontrol et.")
    except Exception as e:
        log(f"[firewall] otomatik izin eklenirken hata: {e}")

def _local_ipv4_addresses():
    """Bu makinenin sahip olduğu tüm yerel IPv4 adreslerini döndürür (Wi-Fi,
    Ethernet, ve Mobil Hotspot'un oluşturduğu sanal adaptör dahil).

    Neden gerekli: multicast sendto() arayüz belirtilmezse Windows'ta
    genelde VARSAYILAN ROTANIN üzerinden çıkar. Mobil Hotspot açıkken bu
    çoğu zaman hotspot'un kendi arayüzü (genelde 192.168.137.1) DEĞİLDİR -
    yani beacon, hotspot'a bağlanan client'a hiç ulaşmaz. Bu yüzden beacon'ı
    her arayüzden ayrı ayrı gönderiyoruz."""
    addrs = set()
    try:
        _, _, iplist = socket.gethostbyname_ex(socket.gethostname())
        addrs.update(iplist)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        addrs.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    addrs.discard("127.0.0.1")
    return addrs or {"0.0.0.0"}

class BeaconBroadcaster(threading.Thread):
    """wfas_native_client.py'nin Discovery sınıfının beklediği formatta
    multicast beacon yayınlar: PREFIX;hostname;mode;port;k=v;k=v..."""

    def __init__(self, hostname, stream_port, rate, channels, bit_depth, auth_mode):
        super().__init__(daemon=True)
        self.hostname = hostname
        self.stream_port = stream_port
        self.rate = rate
        self.channels = channels
        self.bit_depth = bit_depth
        self.auth_mode = auth_mode
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    def _beacon_msg(self):
        return (f"{DISCOVERY_PREFIX};{self.hostname};UNICAST;{self.stream_port};"
                f"sr={self.rate};ch={self.channels};bd={self.bit_depth};"
                f"auth={self.auth_mode}").encode("ascii")

    def run(self):
        msg = self._beacon_msg()
        last_addrs = None
        while not self._stop.is_set():
            addrs = _local_ipv4_addresses()
            if addrs != last_addrs:
                log(f"[beacon] yayin arayuzleri: {', '.join(sorted(addrs))}")
                last_addrs = addrs
            for ip in addrs:
                try:
                    self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                          socket.inet_aton(ip))
                    self.sock.sendto(msg, (DISCOVERY_GROUP, DISCOVERY_PORT))
                except OSError:
                    pass
            time.sleep(BEACON_INTERVAL)

    def stop(self):
        self._stop.set()
        bye = f"{DISCOVERY_PREFIX};{self.hostname};BYE".encode("ascii")
        for ip in _local_ipv4_addresses():
            try:
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                      socket.inet_aton(ip))
                self.sock.sendto(bye, (DISCOVERY_GROUP, DISCOVERY_PORT))
            except OSError:
                pass
        self.sock.close()

class WfasServer:
    def __init__(self, hostname, key=None, rate=48000, channels=2, bit_depth=16):
        self.hostname = hostname
        self.key = key.encode("utf-8") if key else None
        self.rate = rate
        self.channels = channels
        self.bit_depth = bit_depth
        self.frame_size = channels * (bit_depth // 8)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.bind(("0.0.0.0", 0))
        self.port = self.sock.getsockname()[1]

        self.client = None
        self.client_lock = threading.Lock()
        self.pending = {}
        self.seq = 0
        self.sample_pos = 0

    def _handle_hello(self, msg, addr):
        name = parse_kv(msg, "name") or "?"
        cnonce = parse_kv(msg, "cnonce")
        cproof = parse_kv(msg, "cproof")

        with self.client_lock:
            previous = self.client

        if self.key:
            if not cproof:
                snonce = secrets.token_hex(16)
                self.pending[addr] = (cnonce, snonce)
                sproof = hmac_hex(self.key, f"WFAS-S:{cnonce}:{snonce}")
                reply = f"WFAS_AUTH_REQUIRED;snonce={snonce};sproof={sproof}"
                self.sock.sendto(reply.encode("ascii"), addr)
                return
            saved = self.pending.pop(addr, None)
            if not saved:
                self.sock.sendto(b"WFAS_UNAUTHORIZED", addr)
                return
            saved_cnonce, saved_snonce = saved
            expected = hmac_hex(self.key, f"WFAS-C:{saved_cnonce}:{saved_snonce}")
            if not hmac.compare_digest(cproof, expected):
                self.sock.sendto(b"WFAS_UNAUTHORIZED", addr)
                return

        with self.client_lock:
            self.client = addr
            self.seq = 0
            self.sample_pos = 0
        if previous and previous != addr:
            log(f"[bağlantı] {addr[0]}:{addr[1]} bağlandı (name={name}), "
                f"önceki {previous[0]}:{previous[1]} devre dışı bırakıldı")
        else:
            log(f"[bağlantı] {addr[0]}:{addr[1]} bağlandı (name={name})")
        self.sock.sendto(f"HELLO_ACK;v={PROTOCOL_VERSION}".encode("ascii"), addr)

    def _listen_control(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except OSError:
                return
            try:
                msg = data.decode("ascii", errors="ignore")
            except Exception:
                continue
            if msg.startswith("HELLO_FROM_CLIENT"):
                self._handle_hello(msg, addr)
            elif msg == "CLIENT_BYE":
                with self.client_lock:
                    if self.client == addr:
                        self.client = None
                        log(f"[bağlantı] {addr[0]}:{addr[1]} ayrıldı")

    def start(self):
        threading.Thread(target=self._listen_control, daemon=True).start()

    MAX_PAYLOAD = 1400  # IP parcalanmasini onlemek icin (Wi-Fi/hotspot'ta parca kaybi tum paketi dusurur)

    def send_audio(self, payload: bytes):
        with self.client_lock:
            target = self.client
        if not target:
            return
        step = max(1, self.MAX_PAYLOAD // self.frame_size) * self.frame_size
        for off in range(0, len(payload), step):
            part = payload[off:off + step]
            header = struct.pack(">2sBBHI", b"WF", PROTOCOL_VERSION, 0,
                                  self.seq & 0xFFFF, self.sample_pos & 0xFFFFFFFF)
            self.seq += 1
            self.sample_pos += len(part) // self.frame_size
            try:
                self.sock.sendto(header + part, target)
            except OSError:
                pass

    def send_ping(self):
        with self.client_lock:
            target = self.client
        if target:
            try:
                self.sock.sendto(b"PING", target)
            except OSError:
                pass

    def close(self):
        with self.client_lock:
            target = self.client
        if target:
            try:
                self.sock.sendto(b"BYE", target)
            except OSError:
                pass
        self.sock.close()

def build_icon_image(rgba):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=rgba)
    return img

class VolumeSliderWindow:
    """Tray'den 'Ses Ayarla...' tıklanınca açılan/öne gelen küçük Tkinter
    penceresi - ÜÇ kaydırıcı var:

      1) 'Host Kazancı (gain)' - host'un client'a göndermeden ÖNCE ses
         verisine uyguladığı yazılımsal çarpan (mevcut gain_state, ağ
         gerektirmez, tamamen yerel/anlık). Tavanı client'ın kendi sistem
         sesi - client sesi düşükse burayı %100'e alsan bile ses kısık
         kalır.
      2) 'Client'ın Kendi Sesi' - client makinenin GERÇEK sistem ses
         seviyesi (pactl/amixer). `/volume` endpoint'ine HTTP POST atarak
         uzaktan değiştirilir - sürüklerken ~120ms debounce ile ANLIK
         gönderilir (her pikselde değil, ağı boğmamak için), bırakmayı
         beklemek gerekmez. Sadece bir client bağlıyken çalışır.
      3) 'Gecikme / Jitter Buffer' - client'ın çalmaya başlamadan önce
         biriktirdiği tampon süresi (ms). `/latency` endpoint'ine POST -
         bu, akan bir oturumu ANINDA etkilemez, bağlantı bir sonraki
         kurulduğunda (kopup tekrar bağlanınca) uygulanır - akışı
         ortasından kesip yeniden buffer'lamak kendi başına bir kesinti
         yaratacağı için bu daha güvenli bir davranış.

    Kendi Tk ana döngüsünü AYRI bir thread'de sürekli çalıştırır (tek Tk()
    kökü tek thread'de yaşar - Tkinter kuralı budur); tray'in kendi
    thread'inden gelen 'göster' isteğini thread-safe bir Event ile alıp
    kendi mainloop'u içindeki bir `after()` polling'iyle işler - Tk
    widget'larına başka bir thread'den DOĞRUDAN dokunulmuyor, sadece bu
    Event set ediliyor. Client'a giden HTTP istekleri ayrı thread'lerde
    atılır ki yavaş/cevapsız bir client, Tk arayüzünü dondurmasın.

    Görsel tema: kurulu ise `sv-ttk` (pip install sv-ttk) ile Windows 11
    Fluent/WinUI3'e yakın bir görünüm uygulanır (`pip install sv-ttk`).
    Gerçek WinUI3 (XAML/WinRT) bir Tkinter penceresine gömülemez - sv-ttk
    saf Python ile o görünümü ttk widget'larına taklit eden bir tema,
    birebir aynı değil ama çok yakın. Kurulu değilse sessizce varsayılan
    ttk temasına düşer, hataya düşürmez."""

    DEBOUNCE_MS = 120

    def __init__(self, gain_state, server, client_web_port=8091, control_key=None):
        self.gain_state = gain_state
        self.server = server
        self.client_web_port = client_web_port
        self.control_key = control_key.encode("utf-8") if control_key else None
        self._show_requested = threading.Event()
        self.root = None
        self.win = None
        self.scale_var = None
        self._pct_label = None
        self.client_scale_var = None
        self._client_pct_label = None
        self._client_status_label = None
        self._client_scale_widget = None
        self._client_debounce_id = None
        self.latency_scale_var = None
        self._latency_label = None
        self._latency_debounce_id = None
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(timeout=5)

    def _current_client_ip(self):
        with self.server.client_lock:
            c = self.server.client
        return c[0] if c else None

    def _auth_headers(self, body: bytes):
        """control_key ayarlıysa client'ın beklediği X-WFAS-Ts/X-WFAS-Auth
        imza header'larını üretir - client tarafındaki verify_control_auth
        ile birebir aynı şema (HMAC-SHA256(key, f'{ts}:{body}'))."""
        if not self.control_key:
            return {}
        ts = str(int(time.time()))
        sig = hmac_hex(self.control_key, f"{ts}:{body.decode('utf-8')}")
        return {"X-WFAS-Ts": ts, "X-WFAS-Auth": sig}

    def _send_client_volume(self, pct):
        ip = self._current_client_ip()
        if not ip:
            return
        url = f"http://{ip}:{self.client_web_port}/volume"
        body = json.dumps({"value": int(pct)}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", **self._auth_headers(body)},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log(f"[uyarı] Client'ın kendi sesi ayarlanamadı ({ip}): yetkisiz - "
                    "host ve client'taki --key aynı mı?")
            else:
                log(f"[uyarı] Client'ın kendi sesi ayarlanamadı ({ip}): {e}")
        except Exception as e:
            log(f"[uyarı] Client'ın kendi sesi ayarlanamadı ({ip}): {e}")

    def _send_client_latency(self, ms):
        ip = self._current_client_ip()
        if not ip:
            return
        url = f"http://{ip}:{self.client_web_port}/latency"
        body = json.dumps({"value": int(ms)}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", **self._auth_headers(body)},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log(f"[uyarı] Client'ın gecikme ayarı gönderilemedi ({ip}): yetkisiz - "
                    "host ve client'taki --key aynı mı?")
            else:
                log(f"[uyarı] Client'ın gecikme ayarı gönderilemedi ({ip}): {e}")
        except Exception as e:
            log(f"[uyarı] Client'ın gecikme ayarı gönderilemedi ({ip}): {e}")

    def _fetch_client_state(self):
        """Client'ın o anki gerçek ses seviyesi + gecikme ayarını
        status.json'dan okur. Ayrı thread'de çağrılmalı."""
        ip = self._current_client_ip()
        if not ip:
            return None, None, None
        url = f"http://{ip}:{self.client_web_port}/status.json"
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data.get("volume"), data.get("volume_backend"), data.get("prebuffer_ms")
        except Exception:
            return None, None, None

    def _apply_theme(self, root):
        try:
            import sv_ttk
            sv_ttk.set_theme("dark")
            root.configure(bg="#1c1c1c")
            return True
        except ImportError:
            log("[bilgi] sv-ttk kurulu değil, kaydırıcı penceresi 'clam' temayla açılıyor "
                "(WinUI3/Fluent görünümü için: pip install sv-ttk).")
            # Windows'un varsayılan 'vista'/'xpnative' ttk teması Scale (kaydırıcı)
            # widget'ının trough (yiv) rengini özelleştirmeye izin vermiyor - siyah/
            # çirkin bir çubuk olarak kalıyor. 'clam' teması tam kontrol veriyor,
            # açık gri bir yiv + mavi tutamaç ile normal bir kaydırıcıya benziyor.
            from tkinter import ttk
            style = ttk.Style(root)
            style.theme_use("clam")
            bg = "#f3f3f3"
            root.configure(bg=bg)
            style.configure("TFrame", background=bg)
            style.configure("TLabel", background=bg)
            style.configure("TSeparator", background="#cccccc")
            style.configure("Horizontal.TScale", background=bg, troughcolor="#dcdcdc",
                             sliderthickness=18, sliderlength=18)
            style.map("Horizontal.TScale", background=[("active", bg)])
            return False

    def _run(self):
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError:
            log("[bilgi] Tkinter bulunamadı, kaydırıcı penceresi devre dışı "
                "(tray'deki %100/%75/%50/%25/Sessiz hızlı seçenekleri yine çalışır).")
            self._ready.set()
            return

        self.root = tk.Tk()
        self.root.withdraw()
        self._themed = self._apply_theme(self.root)

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)  # başlık çubuğu/çerçeve yok - native tray flyout gibi
        win.attributes("-topmost", True)
        win.configure(bg="#1c1c1c" if self._themed else "#f3f3f3")
        WIN_W, WIN_H = 340, 420
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        # Ekranın sağ alt köşesi (görev çubuğu tray bölgesinin hemen üstü) - Windows'un
        # kendi ses/wifi flyout'larının çıktığı yere yakın bir konum, taskbar için ~48px pay.
        win.geometry(f"{WIN_W}x{WIN_H}+{sw - WIN_W - 12}+{sh - WIN_H - 56}")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        win.bind("<FocusOut>", lambda e: win.withdraw())
        win.withdraw()
        self.win = win

        outer = ttk.Frame(win, padding=16, relief="solid", borderwidth=1)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Host Kazancı", font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Client'a giden veriye uygulanan yazılımsal çarpan.\n"
                               "Client'ın sistem sesi düşükse tavanı bu belirler.",
                  font=("", 8), foreground="#888", justify="left").pack(anchor="w", pady=(0, 6))

        self.scale_var = tk.DoubleVar(value=self.gain_state.get() * 100)
        pct_label = ttk.Label(outer, text=f"%{int(self.scale_var.get())}")
        self._pct_label = pct_label

        def on_move(val):
            pct = float(val)
            self.gain_state.set(pct / 100.0)
            pct_label.config(text=f"%{int(round(pct))}")

        gain_scale = ttk.Scale(outer, from_=0, to=100, orient="horizontal",
                                variable=self.scale_var, command=on_move, length=290)
        gain_scale.pack(fill="x")
        self._bind_click_to_seek(gain_scale, self.scale_var, 0, 100, on_move)
        pct_label.pack(anchor="e", pady=(0, 14))

        ttk.Separator(outer, orient="horizontal").pack(fill="x", pady=4)

        ttk.Label(outer, text="Client'ın Kendi Sesi", font=("", 10, "bold")).pack(
            anchor="w", pady=(10, 0))
        client_status_label = ttk.Label(outer, text="Client bağlı değil",
                                         font=("", 8), foreground="#888")
        client_status_label.pack(anchor="w", pady=(0, 6))
        self._client_status_label = client_status_label

        self.client_scale_var = tk.DoubleVar(value=0)
        client_pct_label = ttk.Label(outer, text="-")
        self._client_pct_label = client_pct_label

        def on_client_move(val):
            pct = float(val)
            client_pct_label.config(text=f"%{int(round(pct))}")
            if self._client_debounce_id is not None:
                self.win.after_cancel(self._client_debounce_id)
            self._client_debounce_id = self.win.after(
                self.DEBOUNCE_MS,
                lambda: threading.Thread(target=self._send_client_volume,
                                          args=(pct,), daemon=True).start(),
            )

        client_scale = ttk.Scale(outer, from_=0, to=100, orient="horizontal",
                                  variable=self.client_scale_var, command=on_client_move,
                                  length=290, state="disabled")
        client_scale.pack(fill="x")
        self._bind_click_to_seek(client_scale, self.client_scale_var, 0, 100, on_client_move)
        client_pct_label.pack(anchor="e", pady=(0, 14))
        self._client_scale_widget = client_scale

        ttk.Separator(outer, orient="horizontal").pack(fill="x", pady=4)

        ttk.Label(outer, text="Gecikme / Jitter Buffer", font=("", 10, "bold")).pack(
            anchor="w", pady=(10, 0))
        ttk.Label(outer, text="Bir sonraki bağlantıda uygulanır (canlı akışı\n"
                               "kesmemek için anında değil).",
                  font=("", 8), foreground="#888", justify="left").pack(anchor="w", pady=(0, 6))

        self.latency_scale_var = tk.DoubleVar(value=120)
        latency_label = ttk.Label(outer, text="120 ms")
        self._latency_label = latency_label

        def on_latency_move(val):
            ms = int(round(float(val)))
            latency_label.config(text=f"{ms} ms")
            if self._latency_debounce_id is not None:
                self.win.after_cancel(self._latency_debounce_id)
            self._latency_debounce_id = self.win.after(
                self.DEBOUNCE_MS,
                lambda: threading.Thread(target=self._send_client_latency,
                                          args=(ms,), daemon=True).start(),
            )

        latency_scale = ttk.Scale(outer, from_=20, to=500, orient="horizontal",
                                   variable=self.latency_scale_var, command=on_latency_move,
                                   length=290, state="disabled")
        latency_scale.pack(fill="x")
        self._bind_click_to_seek(latency_scale, self.latency_scale_var, 20, 500, on_latency_move)
        latency_label.pack(anchor="e")
        self._latency_scale_widget = latency_scale

        self._ready.set()
        self._poll()
        self.root.mainloop()

    def _bind_click_to_seek(self, scale, var, lo, hi, on_change):
        """ttk.Scale'in VARSAYILAN davranışı, yiv üzerinde bir yere tıklayınca
        tutamacı doğrudan oraya SIÇRATMIYOR - tek bir adım (page increment)
        kaydırıyor (senin gördüğün %70 -> %66 gibi). Bunun yerine tıklanan/
        sürüklenen x koordinatından gerçek değeri hesaplayıp değişkeni
        doğrudan o değere set ediyoruz - "break" döndürerek Tk'nin kendi
        varsayılan tıklama/sürükleme davranışının araya girmesini engelliyoruz,
        yoksa ikisi çakışıp zıplama/titreme yapabilir."""
        def _value_from_event(event):
            w = scale.winfo_width()
            frac = event.x / max(w, 1)
            frac = min(max(frac, 0.0), 1.0)
            return lo + frac * (hi - lo)

        def _apply(event):
            value = _value_from_event(event)
            var.set(value)
            on_change(value)  # command= otomatik tetiklenmeyebilir, garantiye alıyoruz
            return "break"

        scale.bind("<Button-1>", _apply)
        scale.bind("<B1-Motion>", _apply)


    def _refresh_client_state(self):
        """Pencere her açıldığında client'ın gerçek sesini, gecikme
        ayarını ve bağlı olup olmadığını arka planda okuyup arayüzü
        günceller."""
        def worker():
            ip = self._current_client_ip()
            if not ip:
                self.win.after(0, self._apply_client_disconnected)
                return
            vol, backend, prebuf = self._fetch_client_state()
            self.win.after(0, lambda: self._apply_client_state(ip, vol, backend, prebuf))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_client_disconnected(self):
        self._client_status_label.config(text="Client bağlı değil", foreground="#888")
        self._client_scale_widget.state(["disabled"])
        self._client_pct_label.config(text="-")
        self._latency_scale_widget.state(["disabled"])
        self._latency_label.config(text="-")

    def _apply_client_state(self, ip, vol, backend, prebuf):
        if prebuf is not None:
            self._latency_scale_widget.state(["!disabled"])
            self.latency_scale_var.set(prebuf)
            self._latency_label.config(text=f"{prebuf} ms")
        else:
            self._latency_scale_widget.state(["disabled"])
            self._latency_label.config(text="-")

        if vol is None:
            self._client_status_label.config(
                text=f"{ip} - ses okunamadı (pactl/amixer yok mu?)", foreground="#e0a800")
            self._client_scale_widget.state(["disabled"])
            self._client_pct_label.config(text="-")
            return
        self._client_status_label.config(text=f"{ip} ({backend})", foreground="#3fb950")
        self._client_scale_widget.state(["!disabled"])
        self.client_scale_var.set(vol)
        self._client_pct_label.config(text=f"%{vol}")

    def _poll(self):
        if self.win is None:
            return
        if self._show_requested.is_set():
            self._show_requested.clear()
            self.scale_var.set(self.gain_state.get() * 100)
            self._pct_label.config(text=f"%{int(round(self.scale_var.get()))}")
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()  # FocusOut'un tetiklenebilmesi için önce odağı alması lazım
            self._refresh_client_state()
        self.root.after(150, self._poll)

    def show(self):
        if self.win is None:
            return  # tkinter yoktu, sessizce yok say
        self._show_requested.set()

class ConnectionWatchdog:
    """Host'un UDP tarafı, client sessizce kaybolursa (elektrik kesintisi,
    WiFi düşmesi, process kill) bunu KENDİLİĞİNDEN fark etmiyor - server.client
    sadece açık bir CLIENT_BYE gelirse ya da yeni bir HELLO ile üzerine
    yazılırsa temizleniyor. Bu sınıf, client'ın kendi durum sunucusundaki
    `/status.json`'ı periyodik olarak yoklayıp (paket sayacının gerçekten
    ilerleyip ilerlemediğine bakarak - phase=='LIVE' + packets artışı) host ve
    client saatleri arasındaki farktan etkilenmeyen, tamamen host'un kendi
    yerel saatine göre bir 'son sağlıklı görülme' zaman damgası tutuyor.

    Ayrı bir thread'de sürekli çalışır, sonucu sadece bir bool olarak
    (is_stale()) dışarı veriyor - ağ gecikmesi/timeout tray'in 1 saniyelik
    ikon güncelleme döngüsünü BLOKLAMASIN diye."""

    def __init__(self, server, client_web_port, stale_after=12, poll_interval=3):
        self.server = server
        self.client_web_port = client_web_port
        self.stale_after = stale_after
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._last_good = None
        self._last_packets = None
        self._last_ip = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            with self.server.client_lock:
                c = self.server.client
            ip = c[0] if c else None
            now = time.time()
            with self._lock:
                if ip != self._last_ip:
                    # yeni client (ya da client yok oldu) - baseline'ı sıfırla,
                    # eski client'ın 'stale' durumu yeni client'a yapışmasın
                    self._last_ip = ip
                    self._last_good = now if ip else None
                    self._last_packets = None
            if ip:
                url = f"http://{ip}:{self.client_web_port}/status.json"
                try:
                    with urllib.request.urlopen(url, timeout=2) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    packets = data.get("packets")
                    phase = data.get("phase")
                    with self._lock:
                        if phase == "LIVE" and packets != self._last_packets:
                            self._last_packets = packets
                            self._last_good = time.time()
                except Exception:
                    pass  # ulaşılamadı - last_good güncellenmiyor, eşiği aşınca stale olur
            time.sleep(self.poll_interval)

    def is_stale(self):
        with self._lock:
            ip = self._last_ip
            last_good = self._last_good
        if not ip or last_good is None:
            return False  # client yok, ya da daha ilk ölçüm bile alınmadı - erken alarm verme
        return (time.time() - last_good) > self.stale_after

def start_tray(app_name, server, stop_event, gain_state, client_web_port=8091, control_key=None):
    """Tray ikonunu ayrı thread'lerde başlatır; konsol/log çıktısı bu
    sırada normal şekilde akmaya devam eder. pystray/Pillow kurulu değilse
    (ya da Linux'ta uygun bir GUI backend'i bulunamazsa) sessizce
    console-only moda düşer, host'un çalışmasını engellemez."""
    try:
        import pystray
    except ImportError:
        log("[bilgi] Tray ikonu için: pip install pystray pillow (opsiyonel, konsoldan devam ediliyor)")
        return None

    try:
        icon_off = build_icon_image((200, 60, 60, 255))
        icon_on = build_icon_image((60, 180, 90, 255))
        icon_stale = build_icon_image((230, 170, 30, 255))
    except ImportError:
        log("[bilgi] Tray ikonu için Pillow gerekli: pip install pillow (konsoldan devam ediliyor)")
        return None

    watchdog = ConnectionWatchdog(server, client_web_port, stale_after=12)

    def _status_text(item=None):
        with server.client_lock:
            c = server.client
        if not c:
            return "Durum: Bekleniyor..."
        if watchdog.is_stale():
            return f"Durum: {c[0]} - YANIT VERMİYOR (bağlantı kopmuş olabilir)"
        return f"Durum: Bağlı ({c[0]})"

    def _quit(icon, item):
        stop_event.set()
        icon.stop()

    def _set_gain(g):
        def _handler(icon, item):
            gain_state.set(g)
            try:
                icon.update_menu()
            except Exception:
                pass
        return _handler

    def _gain_checked(g):
        return lambda item: abs(gain_state.get() - g) < 1e-6

    slider_win = VolumeSliderWindow(gain_state, server, client_web_port=client_web_port,
                                     control_key=control_key)

    def _open_slider(icon, item):
        slider_win.show()

    menu = pystray.Menu(
        pystray.MenuItem(_status_text, None, enabled=False),
        pystray.MenuItem(f"Yayın: {app_name}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Ses Ayarla... (kaydırıcı)", _open_slider, default=True),
        pystray.MenuItem("Hızlı: %100", _set_gain(1.0), checked=_gain_checked(1.0), radio=True),
        pystray.MenuItem("Hızlı: %75", _set_gain(0.75), checked=_gain_checked(0.75), radio=True),
        pystray.MenuItem("Hızlı: %50", _set_gain(0.5), checked=_gain_checked(0.5), radio=True),
        pystray.MenuItem("Hızlı: %25", _set_gain(0.25), checked=_gain_checked(0.25), radio=True),
        pystray.MenuItem("Hızlı: Sessiz", _set_gain(0.0), checked=_gain_checked(0.0), radio=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Kapat", _quit),
    )
    icon = pystray.Icon("wfas_host", icon_off, "WFAS Host", menu)

    def _updater():
        notified = False
        while not stop_event.is_set():
            time.sleep(1)
            if not notified:

                try:
                    icon.notify(f"WFAS Host servisi başlatıldı, yayına hazır ({app_name}).",
                                "WFAS Host")
                except Exception:
                    pass
                notified = True
            with server.client_lock:
                connected = server.client is not None
            if not connected:
                icon.icon = icon_off
                icon.title = "WFAS Host - Bekleniyor"
                icon._stale_last_notify = None
            elif watchdog.is_stale():
                icon.icon = icon_stale
                icon.title = "WFAS Host - Yanıt vermiyor"
                last_notify = getattr(icon, "_stale_last_notify", None)
                now = time.time()
                if last_notify is None or (now - last_notify) >= 120:
                    try:
                        icon.notify("Client yanıt vermiyor - bağlantı kopmuş olabilir "
                                    "(ya da uzun süredir sessizlik çalıyor).", "WFAS Host")
                    except Exception:
                        pass
                    icon._stale_last_notify = now
            else:
                icon.icon = icon_on
                icon.title = "WFAS Host - Bağlı"
                icon._stale_last_notify = None
            try:
                icon.update_menu()
            except Exception:
                pass

    def _run_icon():
        try:
            icon.run()
        except Exception as e:
            log(f"[bilgi] Tray ikonu başlatılamadı ({e}); konsoldan devam ediliyor. "
                  f"Linux'ta gerekirse: sudo apt-get install python3-gi gir1.2-appindicator3-0.1 "
                  f"(veya pip install python3-xlib)")

    threading.Thread(target=_updater, daemon=True).start()
    threading.Thread(target=_run_icon, daemon=True).start()
    return icon

def main():
    ap = argparse.ArgumentParser(description="WFAS taşınabilir host (sistem sesi -> UDP yayın)")
    ap.add_argument("--key", help="Key mode güvenlik anahtarı (verilirse client'ta da aynısı girilmeli)")
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--channels", type=int, default=2)
    ap.add_argument("--name", default=socket.gethostname(), help="Discovery'de görünecek ad")
    ap.add_argument("--no-tray", action="store_true", help="Tray ikonunu devre dışı bırak, sadece konsol")
    ap.add_argument("--client-web-port", type=int, default=8091,
                     help="Client'ın wfas_native_client.py durum sunucusunun portu (varsayılan: "
                          "8091) - tray'deki 'Client'ın Kendi Sesi' kaydırıcısı client'a bu "
                          "porttan HTTP isteği atar, client tarafında --web-port değiştirildiyse "
                          "burada da aynısını ver.")
    ap.add_argument("--no-virtual-speaker", action="store_true",
                     help="Sanal hoparlör otomasyonunu kapat (host'un kendi fiziksel çıkışından "
                          "da normal şekilde ses çıkmaya devam etsin istiyorsan). Sadece Linux'ta "
                          "bir şey değiştirir: PulseAudio/PipeWire null-sink (pactl gerekir), "
                          "varsayılan AÇIK - host cihazdan ses çıkmaz. Windows'ta otomasyon yok; "
                          "host'u sessize almak istiyorsan Ayarlar > Ses'ten kullanılmayan bir "
                          "çıkışı (ör. boş kulaklık jack'i) elle varsayılan yap - bu bayrak "
                          "Windows'ta hiçbir şeyi değiştirmez.")
    ap.add_argument("--hotspot-ssid", default=DEFAULT_HOTSPOT_SSID,
                     help=f"Otomatik acilacak Mobil Hotspot SSID'si (varsayilan: '{DEFAULT_HOTSPOT_SSID}')")
    ap.add_argument("--hotspot-password", default=DEFAULT_HOTSPOT_PASSWORD,
                     help="Yukarıdaki SSID icin parola, en az 8 karakter (client'taki --wifi-password ile ayni olmali)")
    ap.add_argument("--no-hotspot", action="store_true",
                     help="Otomatik Mobil Hotspot acmayi kapat (mevcut bir AG'a elle baglanip kullanmak istiyorsan)")
    args = ap.parse_args()

    log(f"[bilgi] platform: {'Windows' if IS_WINDOWS else 'Linux'}")

    stop_event = threading.Event()

    ensure_firewall_rule()
    if not args.no_hotspot:
        ensure_mobile_hotspot(args.hotspot_ssid, args.hotspot_password)
        threading.Thread(target=hotspot_watchdog,
                          args=(args.hotspot_ssid, args.hotspot_password, stop_event),
                          daemon=True).start()

    if not args.key:
        log("[uyarı] Key modu KAPALI. Paylaşımlı/okul ağında herkes yayınını görüp bağlanabilir. "
              "Önerimiz: --key gizliAnahtar ile başlat, client'ta da aynı anahtarı kullan (--key).")

    virtual_sink = None
    if not args.no_virtual_speaker and not IS_WINDOWS:
        virtual_sink = LinuxVirtualSink()
        try:
            virtual_sink.enable()
        except Exception as e:
            log(f"[uyarı] Sanal hoparlör açılamadı, normal çıkışla devam ediliyor: {e}")
            virtual_sink = None
    elif not args.no_virtual_speaker and IS_WINDOWS:
        log("[bilgi] Windows'ta otomatik sanal hoparlör yok - host'un kendi sesini "
            "susturmak istiyorsan Ayarlar > Ses'ten host'ta kullanılmayan bir çıkışı "
            "(ör. boş kulaklık jack'i) elle varsayılan yap.")

    capture = make_capture(args.rate, args.channels)
    capture.start()
    rate = getattr(capture, "rate", args.rate)
    channels = getattr(capture, "channels", args.channels)

    server = WfasServer(args.name, key=args.key, rate=rate, channels=channels, bit_depth=16)
    server.start()

    beacon = BeaconBroadcaster(args.name, server.port, rate, channels, 16,
                                "key" if args.key else "off")
    beacon.start()

    log(f"[bilgi] {args.name} olarak {rate} Hz / {channels} kanal ile yayına başlandı "
          f"(streaming port {server.port})")
    log("[bilgi] Client tarafında:  python3 wfas_native_client.py   (otomatik keşif)")
    log(f"        ya da elle:       python3 wfas_native_client.py --host <bu_pc_ip> --port {server.port}")
    if args.key:
        log(f"        Key modu açık:     client'ta --key {args.key} eklemeyi unutma")

    gain_state = GainState(1.0)
    tray_icon = None
    if not args.no_tray:
        tray_icon = start_tray(args.name, server, stop_event, gain_state,
                                client_web_port=args.client_web_port, control_key=args.key)

    bytes_per_chunk = int(rate * CHUNK_MS / 1000) * channels * 2
    got_first = False
    chunks = 0
    last_stat = time.time()
    try:
        while not stop_event.is_set():
            data = capture.read(bytes_per_chunk)
            now = time.time()
            if now - last_stat >= 15:
                with server.client_lock:
                    connected = server.client is not None
                if connected:
                    log(f"[akış] son 15 sn: {chunks} ses parçası yakalandı"
                        + ("" if chunks else " (loopback'ten veri gelmiyor: host'ta o an ses çalmıyor "
                           "ya da varsayılan çıkış aygıtı değişmiş olabilir)"))
                chunks = 0
                last_stat = now
            if data:
                chunks += 1
                if not got_first:
                    got_first = True
                    log("[capture] ilk ses verisi yakalandı")
                gain = gain_state.get()
                if gain != 1.0:
                    data = apply_gain(data, gain)
                server.send_audio(data)
            else:
                server.send_ping()
    except KeyboardInterrupt:
        pass
    finally:
        log("kapatılıyor...")
        stop_event.set()
        if tray_icon:
            tray_icon.stop()
        capture.stop()
        if virtual_sink:
            virtual_sink.disable()
        beacon.stop()
        server.close()

if __name__ == "__main__":
    main()
