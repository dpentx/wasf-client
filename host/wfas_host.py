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

        def _cb(in_data, frame_count, time_info, status):
            self._buf.put(in_data)
            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=default_speakers["index"],
            frames_per_buffer=int(self.rate * CHUNK_MS / 1000),
            stream_callback=_cb,
        )
        self._stream.start_stream()

    def read(self, nbytes):
        try:
            return self._buf.get(timeout=1.0)
        except queue.Empty:
            return None

    def stop(self):
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
        if self._pa:
            self._pa.terminate()

def make_capture(rate, channels):
    return WindowsCapture(rate, channels) if IS_WINDOWS else LinuxCapture(rate, channels)

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

def _firewall_rule_exists(name):
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
            capture_output=True, encoding='utf-8', errors='ignore', timeout=5, creationflags=_NO_WINDOW,
        )

        return r.returncode == 0 and name in r.stdout
    except Exception:

        return False

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

    if _firewall_rule_exists(rule_udp) and _firewall_rule_exists(rule_tcp):
        return

    log(f"[firewall] '{base}' icin gelen (inbound) izin bulunamadi, ekleniyor...")

    def _add_args(name, proto):
        return ["advfirewall", "firewall", "add", "rule",
                f"name={name}", "dir=in", "action=allow", f"protocol={proto}",
                f"program={exe_path}", "enable=yes", "profile=any"]

    if _is_admin():

        try:
            subprocess.run(["netsh"] + _add_args(rule_udp, "UDP"),
                            capture_output=True, encoding='utf-8', errors='ignore',
                            timeout=10, creationflags=_NO_WINDOW)
            subprocess.run(["netsh"] + _add_args(rule_tcp, "TCP"),
                            capture_output=True, encoding='utf-8', errors='ignore',
                            timeout=10, creationflags=_NO_WINDOW)
            log("[firewall] izin eklendi.")
        except Exception as e:
            log(f"[firewall] izin eklenemedi: {e}")
        return

    try:
        import tempfile
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="wfas_fw_")
        os.close(fd)
        with open(bat_path, "w", encoding="mbcs", errors="ignore") as f:
            f.write("@echo off\r\n")
            f.write('netsh ' + " ".join(f'"{a}"' if " " in a else a
                                         for a in _add_args(rule_udp, "UDP")) + "\r\n")
            f.write('netsh ' + " ".join(f'"{a}"' if " " in a else a
                                         for a in _add_args(rule_tcp, "TCP")) + "\r\n")
        ok = _run_elevated_and_wait(bat_path, "")
        try:
            os.remove(bat_path)
        except OSError:
            pass
        if ok and _firewall_rule_exists(rule_udp):
            log("[firewall] izin eklendi.")
        else:
            log("[firewall] izin eklenemedi (UAC reddedildi olabilir). "
                "Windows ilk bağlantıda kendi izin penceresini gösterebilir; "
                "çıkarsa 'İzin ver' de.")
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

    def send_audio(self, payload: bytes):
        with self.client_lock:
            target = self.client
        if not target:
            return
        header = struct.pack(">2sBBHI", b"WF", PROTOCOL_VERSION, 0,
                              self.seq & 0xFFFF, self.sample_pos & 0xFFFFFFFF)
        self.seq += 1
        self.sample_pos += len(payload) // self.frame_size
        try:
            self.sock.sendto(header + payload, target)
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

def start_tray(app_name, server, stop_event, gain_state):
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
    except ImportError:
        log("[bilgi] Tray ikonu için Pillow gerekli: pip install pillow (konsoldan devam ediliyor)")
        return None

    def _status_text(item=None):
        with server.client_lock:
            c = server.client
        return f"Durum: {'Bağlı (' + c[0] + ')' if c else 'Bekleniyor...'}"

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

    volume_menu = pystray.Menu(
        pystray.MenuItem("%100", _set_gain(1.0), checked=_gain_checked(1.0), radio=True),
        pystray.MenuItem("%75", _set_gain(0.75), checked=_gain_checked(0.75), radio=True),
        pystray.MenuItem("%50", _set_gain(0.5), checked=_gain_checked(0.5), radio=True),
        pystray.MenuItem("%25", _set_gain(0.25), checked=_gain_checked(0.25), radio=True),
        pystray.MenuItem("Sessiz", _set_gain(0.0), checked=_gain_checked(0.0), radio=True),
    )

    menu = pystray.Menu(
        pystray.MenuItem(_status_text, None, enabled=False),
        pystray.MenuItem(f"Yayın: {app_name}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Ses Seviyesi", volume_menu),
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
            icon.icon = icon_on if connected else icon_off
            icon.title = "WFAS Host - Bağlı" if connected else "WFAS Host - Bekleniyor"
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
        tray_icon = start_tray(args.name, server, stop_event, gain_state)

    bytes_per_chunk = int(rate * CHUNK_MS / 1000) * channels * 2
    try:
        while not stop_event.is_set():
            data = capture.read(bytes_per_chunk)
            if data:
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
        beacon.stop()
        server.close()

if __name__ == "__main__":
    main()
