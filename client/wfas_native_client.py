#!/usr/bin/env python3
"""
WFAS native UDP unicast client.

Uygulamanın kendi WFAS protokolünü (raw 16-bit PCM, UDP) doğrudan konuşur.
Chromium/Playwright yok; ses doğrudan `aplay` (ALSA) üzerinden çalar. Bu
yüzden hem daha az gecikme hem de çok daha az kaynak kullanımı sağlar.

ÖNEMLİ — sunucu tarafında gerekenler:
  1) Ayarlar'da "Advertise HTTP stream" KAPALI olmalı (açıkken uygulama
     Multicast'i zorunlu kılıyor, Unicast anahtarı gri/kilitli görünür).
  2) Multicast anahtarı KAPALI olmalı, yani sunucu Unicast modunda olmalı.
Bu client sadece Unicast'i destekler; sunucu Multicast yayınlıyorsa bunu
tespit edip ekranda/web arayüzünde uyarı gösterir.

Güvenlik modları: Off tam otomatik çalışır. Ask modunda sunucudaki onay
diyaloğunu bekler (WFAS_PENDING keep-alive, ~120 sn üst sınır). Key modunda
--key ile paylaşılan anahtarı ver (HMAC-SHA256 challenge-response). Bölüm 8
(ChaCha20-Poly1305 şifreleme) bu client'ta uygulanmadı — LAN'da güvenlik
kapalıysa (varsayılan) sorun değil, Key modunu kimlik doğrulama için
kullanabilirsin ama trafik şifrelenmez.

Kurulum:
    sudo apt-get install alsa-utils     # aplay için (genelde zaten kurulu)

Kullanım:
    python3 wfas_native_client.py                        # otomatik keşif
    python3 wfas_native_client.py --host 192.168.1.50 --port 5000
    python3 wfas_native_client.py --key gizliAnahtar      # Key modu
    python3 wfas_native_client.py --web-port 8091
"""
import argparse
import hashlib
import hmac
import json
import queue
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DISCOVERY_GROUP = "239.255.0.1"
DISCOVERY_PORT = 9091
DISCOVERY_PREFIX = "WIFI_AUDIO_STREAMER_DISCOVERY"
PROTOCOL_VERSION = 2
FRESH_WINDOW = 30
PING_TIMEOUT = 3.0
MIN_PREBUFFER_MS = 20
MAX_PREBUFFER_MS = 1000

# AudioWriter, YENİ BİR OTURUM başlarken (bağlantı koptuğunda/tekrar
# bağlanıldığında) bu değeri okur. Yani host'tan gelen bir değişiklik o an
# akan sese anında değil, bir sonraki oturuma yansır - ses akışını
# ortasından kesip yeniden buffer'lamak zaten kendi başına küçük bir
# kesinti/gecikme yaratacağı için bu, akışı bozmadan en makul davranış.
prebuffer_lock = threading.Lock()
current_prebuffer_ms = 120

def get_prebuffer_ms():
    with prebuffer_lock:
        return current_prebuffer_ms

def set_prebuffer_ms(ms):
    global current_prebuffer_ms
    ms = max(MIN_PREBUFFER_MS, min(MAX_PREBUFFER_MS, int(ms)))
    with prebuffer_lock:
        current_prebuffer_ms = ms
    set_state(prebuffer_ms=ms)
    return ms

state_lock = threading.Lock()
state = {
    "phase": "IDLE",
    "server": None,
    "sr": None, "ch": None,
    "packets": 0, "silence_inserted": 0,
    "last_error": None,
    "volume": None, "volume_backend": None,
    "prebuffer_ms": current_prebuffer_ms,
    "updated": time.time(),
}

def set_state(**kwargs):
    with state_lock:
        state.update(kwargs)
        state["updated"] = time.time()

def get_state():
    with state_lock:
        return dict(state)

class SystemVolume:
    """Client makinenin KENDİ ses seviyesini (host'un gönderdiği veriye
    uyguladığı yazılımsal kazanç DEĞİL - donanım/OS seviyesindeki gerçek
    çıkış seviyesi) okur/yazar. Bunun host tarafındaki gain'den farkı: host
    gain'i sadece 0-100 arası AŞAĞI kısar (kaynağı çarpıp gönderiyor),
    client'ın kendi ses seviyesi ise gerçek tavanı belirler - client'ın
    sistem sesi düşükse host tray'i %100'e alsan bile ses kısık kalır.
    İkisi birlikte kullanılır: client sesi (bu sınıf) tabanı/tavanı
    ayarlar, host'taki gain ince ayar/hızlı kısma için kalır.

    MX Linux/Fluxbox + systemd ortamında iki olası backend var, hangisi
    kuruluysa (PulseAudio/PipeWire varsa pactl, yoksa çıplak ALSA) onu
    kullanır - ikisi de yoksa sessizce None döner, hataya düşürmez."""

    def __init__(self):
        self._backend = None
        if shutil.which("pactl"):
            self._backend = "pactl"
        elif shutil.which("amixer"):
            self._backend = "amixer"
        if self._backend is None:
            print("[ses] pactl da amixer de bulunamadı - client'ın kendi ses "
                  "seviyesi okunamıyor/değiştirilemiyor. PulseAudio/PipeWire "
                  "içinse 'pactl' (pulseaudio-utils paketi), çıplak ALSA "
                  "içinse 'amixer' (alsa-utils paketi) kurulu olmalı.")

    @property
    def backend(self):
        return self._backend

    def get(self):
        """0-100 arası tam sayı döner, okunamazsa None."""
        if self._backend == "pactl":
            try:
                out = subprocess.check_output(
                    ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                    text=True, timeout=3,
                )
                m = re.search(r"(\d+)%", out)
                return int(m.group(1)) if m else None
            except Exception:
                return None
        if self._backend == "amixer":
            try:
                out = subprocess.check_output(
                    ["amixer", "get", "Master"], text=True, timeout=3,
                )
                m = re.search(r"\[(\d+)%\]", out)
                return int(m.group(1)) if m else None
            except Exception:
                return None
        return None

    def set(self, pct):
        """0-100 arası hedef seviye. Başarılıysa True döner. 0'a çekerken
        mute de uygular (bazı donanımlarda %0 ile mute farklı davranabiliyor
        - ikisini birden yapmak sağlamlaştırıyor), 0 üzerine çıkınca unmute
        eder."""
        pct = max(0, min(100, int(round(pct))))
        if self._backend == "pactl":
            try:
                subprocess.run(
                    ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"],
                    timeout=3, check=True,
                )
                subprocess.run(
                    ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if pct == 0 else "0"],
                    timeout=3,
                )
                return True
            except Exception as e:
                print(f"[ses] pactl ile seviye ayarlanamadı: {e}")
                return False
        if self._backend == "amixer":
            try:
                subprocess.run(
                    ["amixer", "-q", "set", "Master", f"{pct}%",
                     "mute" if pct == 0 else "unmute"],
                    timeout=3, check=True,
                )
                return True
            except Exception as e:
                print(f"[ses] amixer ile seviye ayarlanamadı: {e}")
                return False
        return False

system_volume = SystemVolume()

def volume_poll_loop(interval=3.0):
    """Arka planda periyodik olarak gerçek sistem sesini okuyup state'e
    yazar - biri terminalden/masaüstünden elle değiştirse bile web
    arayüzü/host güncel değeri görür."""
    while True:
        set_state(volume=system_volume.get(), volume_backend=system_volume.backend)
        time.sleep(interval)


class Discovery:
    """WFAS UDP multicast beacon'ını sürekli dinler; en güncel sunucu
    bilgisini (ip, native streaming portu, mode, sr/ch/bd, auth) tutar."""

    def __init__(self, host_filter=None):
        self.lock = threading.Lock()
        self.info = None
        self.last_seen = 0.0
        self._sock = None
        self._stop = threading.Event()
        self.host_filter = host_filter

    def start(self):
        threading.Thread(target=self._start_with_retry, daemon=True).start()

    def _start_with_retry(self):
        """Multicast gruba katilma, agin (ozellikle WiFi/DHCP) hazir olmasini
        bekleyebilir - boot aninda hemen basarisiz olursa cok kisa surede
        tekrar dener, programi cokertmez (systemd'nin --user servisleri
        network-online.target'i guvenilir sekilde beklemez, bu yuzden bu
        retry burada, kod seviyesinde olmali)."""
        delay = 1
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", DISCOVERY_PORT))
                mreq = struct.pack("4sl", socket.inet_aton(DISCOVERY_GROUP), socket.INADDR_ANY)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                sock.settimeout(1.0)
                self._sock = sock
                print(f"[discovery] {DISCOVERY_GROUP}:{DISCOVERY_PORT} sürekli dinleniyor...")
                self._loop()
                return
            except OSError as e:
                print(f"[discovery] ağ henüz hazır değil ({e}), {delay}sn sonra tekrar denenecek...")
                self._stop.wait(delay)
                delay = min(delay * 2, 15)

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if self.host_filter and addr[0] != self.host_filter:
                continue
            msg = data.decode("ascii", errors="ignore")
            parts = msg.split(";")
            if not parts or parts[0] != DISCOVERY_PREFIX:
                continue
            if len(parts) < 4 or parts[2] == "BYE":
                continue
            info = {"ip": addr[0], "hostname": parts[1], "mode": parts[2], "port": None}
            try:
                info["port"] = int(parts[3])
            except ValueError:
                continue
            for tok in parts[4:]:
                if "=" not in tok:
                    continue
                k, v = tok.split("=", 1)
                if k in ("sr", "ch", "bd", "http_port"):
                    try:
                        info[k] = int(v)
                    except ValueError:
                        pass
                elif k in ("auth", "enc", "mic", "protocols"):
                    info[k] = v
            with self.lock:
                if self.info != info:
                    print(f"[discovery] sunucu: {info}")
                self.info = info
                self.last_seen = time.time()

    def snapshot(self):
        with self.lock:
            return self.info, self.last_seen

    def stop(self):
        self._stop.set()
        if self._sock:
            self._sock.close()

def parse_kv(msg, key):
    for tok in msg.split(";"):
        if tok.startswith(key + "="):
            return tok.split("=", 1)[1]
    return None

def hmac_hex(key: bytes, msg: str) -> str:
    return hmac.new(key, msg.encode("ascii"), hashlib.sha256).hexdigest()

CONTROL_AUTH_WINDOW = 30  # saniye - bu pencerenin dışındaki imzalar reddedilir (replay koruması)
control_key = None  # main()'de --key'den set edilir (bytes ya da None)

def verify_control_auth(headers, raw_body: bytes) -> bool:
    """/volume, /latency, /connect için basit paylaşılan-anahtar imza
    kontrolü - ses akışındaki --key ile AYNI anahtarı kullanır (kasıtlı,
    ayrı bir anahtar yönetmek gereksiz karmaşıklık olurdu). --key
    verilmediyse (Open mode) kontrol endpoint'leri de açık kalır - ses
    protokolüyle tutarlı davranış, sürpriz olmasın diye.
    İmza: HMAC-SHA256(key, f"{ts}:{body}") - X-WFAS-Ts / X-WFAS-Auth
    header'larında gelir. ts, host'un sistem saatiyle ±30 sn içinde
    olmalı (replay koruması); host ile client'ın saatleri epey
    uyumsuzsa bu payı gerekirse büyütürüz."""
    if control_key is None:
        return True
    ts = headers.get("X-WFAS-Ts")
    sig = headers.get("X-WFAS-Auth")
    if not ts or not sig:
        return False
    try:
        ts_val = int(ts)
    except ValueError:
        return False
    if abs(time.time() - ts_val) > CONTROL_AUTH_WINDOW:
        return False
    expected = hmac_hex(control_key, f"{ts}:{raw_body.decode('utf-8', 'replace')}")
    return hmac.compare_digest(sig, expected)


class WifiAutoConnector(threading.Thread):
    """Host taşınabilir olduğu ve her seferinde farklı bir laptop olabildiği
    için, host kendi Mobil Hotspot'unu hep aynı SSID ile ("wfas wifi")
    yayınlıyor. Bu thread arka planda periyodik olarak o SSID'yi tarar,
    görürse (NetworkManager/nmcli üzerinden) otomatik bağlanır - hangi
    laptop host olursa olsun elle WiFi ayarlarına girmek gerekmez.

    nmcli yoksa (NetworkManager kullanılmıyorsa) ya da izin sorunu varsa
    sessizce pes eder; mevcut discovery/--host akışını hiçbir şekilde
    engellemez, sadece bir kolaylık katmanıdır."""

    def __init__(self, ssid, password, scan_interval=8):
        super().__init__(daemon=True)
        self.ssid = ssid
        self.password = password
        self.scan_interval = scan_interval
        self._stop = threading.Event()
        self._nmcli_ok = None

    def _have_nmcli(self):
        if self._nmcli_ok is None:
            try:
                subprocess.run(["nmcli", "-v"], capture_output=True, timeout=3)
                self._nmcli_ok = True
            except Exception:
                self._nmcli_ok = False
                print("[wifi] nmcli bulunamadı, otomatik WiFi bağlanma devre dışı "
                      "(NetworkManager kurulu değilse bu normal - elle bağlanabilirsin).")
        return self._nmcli_ok

    def _currently_connected_ssid(self):
        try:
            r = subprocess.run(
                ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                if line.startswith("yes:") or line.startswith("evet:"):
                    return line.split(":", 1)[1]
        except Exception:
            pass
        return None

    def _ssid_visible(self):
        try:
            r = subprocess.run(
                ["nmcli", "-t", "-f", "ssid", "dev", "wifi", "list", "--rescan", "yes"],
                capture_output=True, text=True, timeout=10,
            )
            return any(line.strip() == self.ssid for line in r.stdout.splitlines())
        except Exception:
            return False

    def _connect(self):
        try:
            args = ["nmcli", "device", "wifi", "connect", self.ssid]
            if self.password:
                args += ["password", self.password]
            r = subprocess.run(args, capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                print(f"[wifi] '{self.ssid}' ağına bağlanıldı.")
                return True
            print(f"[wifi] '{self.ssid}' ağına bağlanılamadı: {r.stderr.strip() or r.stdout.strip()}")
        except Exception as e:
            print(f"[wifi] bağlanma denemesi başarısız: {e}")
        return False

    def run(self):
        if not self._have_nmcli():
            return
        while not self._stop.is_set():
            try:
                current = self._currently_connected_ssid()
                if current != self.ssid and self._ssid_visible():
                    self._connect()
            except Exception:
                pass
            self._stop.wait(self.scan_interval)

    def stop(self):
        self._stop.set()

class Session:
    def __init__(self, ip, port, key=None, device_name="MX-Linux-Receiver"):
        self.ip = ip
        self.port = port
        self.key = key.encode("utf-8") if key else None
        self.device_name = device_name
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        self.sock.settimeout(1.0)

    def close(self):
        try:
            self.sock.sendto(b"CLIENT_BYE", (self.ip, self.port))
        except OSError:
            pass
        self.sock.close()

    def _send_hello(self, cnonce=None, cproof=None):
        msg = f"HELLO_FROM_CLIENT;v={PROTOCOL_VERSION};name={self.device_name}"
        if cnonce:
            msg += f";cnonce={cnonce}"
        if cproof:
            msg += f";cproof={cproof}"
        self.sock.sendto(msg.encode("ascii"), (self.ip, self.port))

    def handshake(self, quick_timeout=5.0, pending_cap=125.0):
        """Unicast HELLO/ACK el sıkışması. Off/Ask/Key modlarının hepsini
        destekler (bkz. WFAS_PROTOCOL.md §5, §7)."""
        cnonce = secrets.token_hex(16)
        self._send_hello(cnonce=cnonce)
        deadline = time.time() + quick_timeout
        pending_watchdog = None

        while time.time() < deadline:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                if pending_watchdog and time.time() - pending_watchdog > 6:
                    raise TimeoutError("WFAS_PENDING zaman aşımı (sunucuda onay gelmedi)")
                continue
            if addr[0] != self.ip:
                continue
            msg = data.decode("ascii", errors="ignore")

            if msg.startswith("HELLO_ACK;v="):
                v = int(parse_kv(msg, "v") or -1)
                if v != PROTOCOL_VERSION:
                    raise RuntimeError(f"Protokol uyuşmazlığı: sunucu v{v}, biz v{PROTOCOL_VERSION}")
                return

            if msg.startswith("WFAS_INCOMPATIBLE"):
                raise RuntimeError(f"Protokol uyuşmazlığı: sunucu v{parse_kv(msg, 'v')}")

            if msg.startswith("WFAS_BUSY"):
                raise RuntimeError("Sunucu başka bir cihaza akış veriyor (Unicast tek client alır)")

            if msg.startswith("WFAS_PENDING"):
                set_state(phase="PENDING")
                pending_watchdog = time.time()
                deadline = max(deadline, time.time() + pending_cap)
                continue

            if msg.startswith("WFAS_AUTH_REQUIRED"):
                if not self.key:
                    raise RuntimeError("Sunucu anahtar istiyor (Key mode); --key ile anahtar ver")
                snonce = parse_kv(msg, "snonce")
                sproof = parse_kv(msg, "sproof")
                expected = hmac_hex(self.key, f"WFAS-S:{cnonce}:{snonce}")
                if not hmac.compare_digest(sproof or "", expected):
                    raise RuntimeError("Sunucu kimliği doğrulanamadı (yanlış anahtar / sahte sunucu)")
                cproof = hmac_hex(self.key, f"WFAS-C:{cnonce}:{snonce}")
                self._send_hello(cnonce=cnonce, cproof=cproof)
                continue

            if msg.startswith("WFAS_UNAUTHORIZED"):
                raise RuntimeError("Bağlantı reddedildi (yanlış anahtar / reddedildi / zaman aşımı)")

        raise TimeoutError("El sıkışma zaman aşımına uğradı (sunucu yanıt vermedi)")

    def stream(self, writer):
        """PCM paketlerini ayrıştırır, kayıp/sıra dışı paketleri sample
        position'a göre sessizlikle telafi eder, PING/BYE'ı işler.
        `writer`, AudioWriter örneğidir; gerçek `aplay` yazımı ayrı bir
        thread'de yapılır ki ALSA gecikmesi bu recv döngüsünü bloklamasın
        (kesik ses'in en sık nedeni budur)."""
        last_activity = time.time()
        first_packet = True
        last_sample_pos = None
        last_frame_count = 0
        seq_seen = None

        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                if time.time() - last_activity > PING_TIMEOUT:
                    raise TimeoutError("Sunucudan 3 sn içinde veri gelmedi")
                continue
            if addr[0] != self.ip:
                continue
            last_activity = time.time()

            if len(data) >= 10 and data[0:2] == b"WF":
                version = data[2]
                if first_packet:
                    if version != PROTOCOL_VERSION:
                        raise RuntimeError(f"Protokol uyuşmazlığı (ilk paket): sunucu v{version}")
                    first_packet = False
                seq = struct.unpack(">H", data[4:6])[0]
                sample_pos = struct.unpack(">I", data[6:10])[0]
                payload = data[10:]

                if seq_seen is not None:
                    diff = (seq - seq_seen) & 0xFFFF
                    if diff == 0 or diff > 0x8000:
                        continue
                seq_seen = seq

                if last_sample_pos is not None:
                    expected = last_sample_pos + last_frame_count
                    gap = sample_pos - expected
                    if 0 < gap < 48000 * 2:
                        writer.push(b"\x00" * (gap * writer.frame_size))
                        with state_lock:
                            state["silence_inserted"] += gap

                if payload:
                    writer.push(payload)
                    last_frame_count = len(payload) // writer.frame_size
                else:
                    last_frame_count = 0
                last_sample_pos = sample_pos

                with state_lock:
                    state["seq"] = seq
                    state["packets"] += 1
                continue

            msg = data.decode("ascii", errors="ignore")
            if msg == "PING":
                continue
            if msg.startswith("BYE"):
                raise ConnectionResetError("Sunucu akışı durdurdu (BYE)")

class AplayPlayer:
    """Ham PCM'i doğrudan `aplay`in stdin'ine yazan basit oynatıcı."""

    def __init__(self, sample_rate, channels, bit_depth=16):
        self.channels = channels
        self.frame_size = channels * (bit_depth // 8)
        self.proc = subprocess.Popen(
            ["aplay", "-q", "-t", "raw", "-f", "S16_LE",
             "-r", str(sample_rate), "-c", str(channels),
             "--buffer-time=200000", "--period-time=50000", "-"],
            stdin=subprocess.PIPE,
        )

    def write(self, data: bytes):
        try:
            self.proc.stdin.write(data)
        except (BrokenPipeError, OSError):
            pass

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()

class AudioWriter:
    """Ağdan gelen PCM'i ayrı bir thread'de `player`'a yazar. UDP recv
    döngüsü asla ALSA/CPU gecikmelerine takılıp bloklanmasın diye araya
    bir kuyruk konur; ayrıca çalmaya başlamadan önce küçük bir jitter
    buffer (varsayılan ~120ms) biriktirir, böylece ufak varış-zamanlaması
    sapmaları sesi kesmez."""

    def __init__(self, player, sample_rate, channels, bit_depth=16,
                 prebuffer_ms=None, max_queue_ms=1500):
        self.player = player
        self.frame_size = channels * (bit_depth // 8)
        self.bytes_per_ms = sample_rate * self.frame_size // 1000
        if prebuffer_ms is None:
            prebuffer_ms = get_prebuffer_ms()  # o anki güncel gecikme ayarı
        self.prebuffer_bytes = self.bytes_per_ms * prebuffer_ms
        self.max_queue_bytes = self.bytes_per_ms * max_queue_ms
        self.q = queue.Queue()
        self.q_bytes = 0
        self.q_lock = threading.Lock()
        self._stop = threading.Event()
        self._started = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def push(self, data: bytes):
        if not data:
            return
        with self.q_lock:
            self.q_bytes += len(data)

            while self.q_bytes > self.max_queue_bytes and not self.q.empty():
                try:
                    old = self.q.get_nowait()
                    self.q_bytes -= len(old)
                except queue.Empty:
                    break
        self.q.put(data)

    def _run(self):
        prebuf = []
        prebuf_bytes = 0
        while not self._stop.is_set():
            try:
                chunk = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            with self.q_lock:
                self.q_bytes -= len(chunk)
            if not self._started:
                prebuf.append(chunk)
                prebuf_bytes += len(chunk)
                if prebuf_bytes >= self.prebuffer_bytes:
                    for b in prebuf:
                        self.player.write(b)
                    prebuf = []
                    self._started = True
                continue
            self.player.write(chunk)

    def stop(self):
        self._stop.set()

STATUS_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<title>WFAS Native Client</title>
<style>
body{font-family:sans-serif;background:#111;color:#eee;padding:24px}
.card{background:#1e1e1e;border:1px solid #333;border-radius:12px;padding:20px;max-width:480px}
.row{display:flex;justify-content:space-between;margin:6px 0;font-size:14px}
.ok{color:#22c55e}.bad{color:#ef4444}.warn{color:#eab308}
input{background:#111;color:#eee;border:1px solid #444;border-radius:6px;padding:6px;margin-right:6px}
button{background:#BB86FC;border:none;border-radius:6px;padding:6px 12px;cursor:pointer}
input[type=range]{width:100%}
</style></head><body>
<div class="card">
<h2>WFAS Native UDP Client</h2>
<div class="row"><span>Durum</span><span id="phase">-</span></div>
<div class="row"><span>Sunucu</span><span id="server">-</span></div>
<div class="row"><span>Format</span><span id="fmt">-</span></div>
<div class="row"><span>Paket</span><span id="pkts">-</span></div>
<div class="row"><span>Sessizlikle doldurulan örnek</span><span id="sil">-</span></div>
<div class="row"><span>Son hata</span><span id="err">-</span></div>
<hr>
<div class="row"><span>Client ses seviyesi (<span id="vbackend">-</span>)</span><span id="vpct">-</span></div>
<input type="range" id="vol" min="0" max="100" value="0" oninput="onSlide(this.value)" onchange="setVolume(this.value)">
<div class="row" style="margin-top:10px"><span>Gecikme / jitter buffer</span><span id="lms">-</span></div>
<input type="range" id="lat" min="20" max="500" step="10" value="120" oninput="onLatSlide(this.value)" onchange="setLatency(this.value)">
<hr>
<div><input id="h" placeholder="IP"><input id="p" placeholder="Port" size="4">
<button onclick="connect()">Bağlan</button></div>
</div>
<script>
let dragging = false;
let latDragging = false;
async function tick(){
  try{
    const r = await fetch('/status.json'); const j = await r.json();
    const el = document.getElementById('phase');
    el.textContent = j.phase;
    el.className = j.phase==='LIVE' ? 'ok' : (j.phase==='ERROR' ? 'bad' : 'warn');
    document.getElementById('server').textContent = j.server || '-';
    document.getElementById('fmt').textContent = (j.sr && j.ch) ? (j.sr+' Hz / '+j.ch+' ch') : '-';
    document.getElementById('pkts').textContent = j.packets;
    document.getElementById('sil').textContent = j.silence_inserted;
    document.getElementById('err').textContent = j.last_error || '-';
    document.getElementById('vbackend').textContent = j.volume_backend || 'yok';
    if(!dragging && j.volume !== null && j.volume !== undefined){
      document.getElementById('vol').value = j.volume;
      document.getElementById('vpct').textContent = '%' + j.volume;
    }
    if(!latDragging && j.prebuffer_ms !== null && j.prebuffer_ms !== undefined){
      document.getElementById('lat').value = j.prebuffer_ms;
      document.getElementById('lms').textContent = j.prebuffer_ms + ' ms';
    }
  }catch(e){}
}
function onSlide(v){
  dragging = true;
  document.getElementById('vpct').textContent = '%' + v;
}
async function setVolume(v){
  const r = await fetch('/volume', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({value: parseInt(v)})});
  if(r.status === 401){ alert('Yetkisiz: Key mode açık, web arayüzünden imzasız kontrol edilemez. Host tray\'ini ya da imzalı bir istemciyi kullan.'); }
  dragging = false;
}
function onLatSlide(v){
  latDragging = true;
  document.getElementById('lms').textContent = v + ' ms';
}
async function setLatency(v){
  const r = await fetch('/latency', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({value: parseInt(v)})});
  if(r.status === 401){ alert('Yetkisiz: Key mode açık, web arayüzünden imzasız kontrol edilemez. Host tray\'ini ya da imzalı bir istemciyi kullan.'); }
  latDragging = false;
}
async function connect(){
  const host=document.getElementById('h').value, port=document.getElementById('p').value;
  if(!host) return;
  await fetch('/connect', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({host, port: parseInt(port||'0')})});
}
setInterval(tick, 1000); tick();
</script></body></html>"""

manual_override = {"host": None, "port": None}
override_lock = threading.Lock()

class StatusHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = STATUS_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/status.json":
            body = json.dumps(get_state()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b""

        if self.path in ("/connect", "/volume", "/latency"):
            if not verify_control_auth(self.headers, raw_body):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                body = json.dumps({"error": "yetkisiz - imza/zaman damgası "
                                             "geçersiz ya da eksik"}).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        if self.path == "/connect":
            try:
                payload = json.loads(raw_body or b"{}")
                with override_lock:
                    manual_override["host"] = payload.get("host") or None
                    manual_override["port"] = int(payload.get("port") or 0) or None
                self.send_response(204)
                self.end_headers()
            except Exception:
                self.send_response(400)
                self.end_headers()
        elif self.path == "/volume":
            try:
                payload = json.loads(raw_body or b"{}")
                pct = int(payload.get("value"))
            except Exception:
                self.send_response(400)
                self.end_headers()
                return
            if system_volume.backend is None:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                body = json.dumps({"error": "pactl/amixer bulunamadı"}).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            ok = system_volume.set(pct)
            set_state(volume=system_volume.get(), volume_backend=system_volume.backend)
            self.send_response(200 if ok else 500)
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"ok": ok, "volume": get_state()["volume"]}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/latency":
            try:
                payload = json.loads(raw_body or b"{}")
                ms = int(payload.get("value"))
            except Exception:
                self.send_response(400)
                self.end_headers()
                return
            applied = set_prebuffer_ms(ms)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"ok": True, "prebuffer_ms": applied,
                                "note": "sonraki oturumda (bağlantı kopup "
                                        "tekrar kurulunca) uygulanır"}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

def start_status_server(port=8091):
    srv = ThreadingHTTPServer(("0.0.0.0", port), StatusHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[web] durum arayüzü: http://localhost:{port}")
    return srv

def main():
    ap = argparse.ArgumentParser(description="WFAS native UDP unicast client")
    ap.add_argument("--host", help="Sunucu IP (verilirse keşif yerine sabit hedef)")
    ap.add_argument("--port", type=int, help="Sunucunun WFAS streaming portu (--host ile birlikte)")
    ap.add_argument("--key", help="Key mode güvenlik anahtarı (sunucuda 'Key' modu açıksa). "
                                   "Aynı anahtar /volume ve /latency uzaktan kontrol "
                                   "endpoint'lerini de korur - verilmezse (Open mode) o "
                                   "endpoint'ler de kimlik doğrulamasız kalır.")
    ap.add_argument("--web-port", type=int, default=8091, help="Durum arayüzü portu (varsayılan 8091)")
    ap.add_argument("--prebuffer-ms", type=int, default=120,
                     help="Çalmaya başlamadan önce biriktirilen jitter buffer / gecikme süresi, "
                          "ms (varsayılan 120). Büyütürsen ağ sapmalarına karşı daha dayanıklı "
                          "olur ama host'ta bir medyayı durdurunca client'ta hissedilen gecikme "
                          "artar; küçültürsen tam tersi. Host tray'inden de uzaktan (yeni "
                          "bağlantıya uygulanır) değiştirilebilir.")
    ap.add_argument("--interval", type=int, default=10, help="Keşif/sağlık kontrol aralığı, sn")
    ap.add_argument("--wifi-ssid", default="wfas wifi",
                     help="Host'un Mobil Hotspot SSID'si - görülünce otomatik bağlanılır (varsayılan: 'wfas wifi')")
    ap.add_argument("--wifi-password", default="wfasaudio1",
                     help="Yukarıdaki SSID için parola (host'taki --hotspot-password ile aynı olmalı)")
    ap.add_argument("--no-wifi-autoconnect", action="store_true",
                     help="Otomatik WiFi bağlanmayı kapat (mevcut ağı elle yönetmek istiyorsan)")
    args = ap.parse_args()

    global control_key
    control_key = args.key.encode("utf-8") if args.key else None

    start_status_server(args.web_port)
    set_prebuffer_ms(args.prebuffer_ms)
    set_state(volume=system_volume.get(), volume_backend=system_volume.backend)
    threading.Thread(target=volume_poll_loop, daemon=True).start()

    wifi_connector = None
    if not args.no_wifi_autoconnect:
        wifi_connector = WifiAutoConnector(args.wifi_ssid, args.wifi_password)
        wifi_connector.start()

    discovery = None
    manual_target = None
    if args.host and args.port:

        manual_target = {"ip": args.host, "port": args.port, "sr": 48000, "ch": 2, "bd": 16}
    elif args.host:

        print(f"[bilgi] --port verilmedi, {args.host} için discovery beacon'ı bekleniyor "
              f"(uygulamanın multicast keşfi açık olmalı)...")
        discovery = Discovery(host_filter=args.host)
        discovery.start()
    else:
        discovery = Discovery()
        discovery.start()

    current = manual_target
    while True:
        with override_lock:
            ov_host, ov_port = manual_override["host"], manual_override["port"]
        if ov_host:
            current = {"ip": ov_host, "port": ov_port or 0, "sr": 48000, "ch": 2, "bd": 16}
            with override_lock:
                manual_override["host"] = None
                manual_override["port"] = None

        if current is None and discovery:
            info, last_seen = discovery.snapshot()
            if info and (time.time() - last_seen) < FRESH_WINDOW:
                current = info

        if current is None:
            set_state(phase="DISCOVERING", server=None)
            time.sleep(args.interval)
            continue

        if not current.get("port"):
            print(f"[hata] geçersiz port (0/boş) - {current.get('ip')} için port bilgisi yok, atlanıyor")
            set_state(phase="ERROR", last_error="Port bilgisi yok (0)")
            current = None
            time.sleep(args.interval)
            continue

        if current.get("mode") == "MULTICAST":
            print("[uyarı] sunucu Multicast modunda yayın yapıyor. Unicast için: "
                  "uygulamada Ayarlar -> 'Advertise HTTP stream' kapat, sonra "
                  "Multicast anahtarını kapatıp Unicast'e al.")
            set_state(phase="ERROR", last_error="Sunucu Multicast modunda")
            time.sleep(args.interval)
            current = None
            continue

        sr = current.get("sr", 48000)
        ch = current.get("ch", 2)
        bd = current.get("bd", 16)
        server_label = f"{current['ip']}:{current['port']}"
        set_state(phase="HANDSHAKE", server=server_label, sr=sr, ch=ch)

        session = Session(current["ip"], current["port"], key=args.key)
        player = None
        writer = None
        try:
            session.handshake()
            player = AplayPlayer(sr, ch, bd)
            writer = AudioWriter(player, sr, ch, bd)
            set_state(phase="LIVE", server=server_label, sr=sr, ch=ch,
                      packets=0, silence_inserted=0, last_error=None)
            print(f"[player] canlı: {server_label} ({sr} Hz, {ch} kanal)")
            session.stream(writer)
        except Exception as e:
            print(f"[hata] {e}")
            set_state(phase="ERROR", last_error=str(e))
        finally:
            session.close()
            if writer:
                writer.stop()
            if player:
                player.close()
        current = None
        time.sleep(min(args.interval, 5))

if __name__ == "__main__":
    main()
