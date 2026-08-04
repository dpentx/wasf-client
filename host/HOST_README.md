# WFAS Taşınabilir Host

Sistem sesini yakalayıp `wfas_native_client.py`'nin konuştuğu WFAS UDP
protokolüyle ağa yayınlayan, kurulum gerektirmeyen bir host.

## Tray ikonu

Varsayılan olarak bir sistem tepsisi (tray) ikonu da açılır — konsol/log
çıktısı bu sırada normal şekilde akmaya devam eder, birbirini engellemez.
Kırmızı nokta = bekleniyor, yeşil nokta = bir client bağlı. Sağ tık menüsünde
durum yazısı ve "Kapat" var.

Gerekli paketler: `pip install pystray pillow` (Windows exe derlerken
`build_windows_exe.bat` bunları zaten otomatik kuruyor). Bu paketler kurulu
değilse ya da Linux'ta uygun bir GUI backend'i bulunamazsa, host tray'siz
(sadece konsol) modda çalışmaya devam eder — hata vermez.

Linux'ta tray ikonu bazı minimal masaüstü ortamlarında (ör. bazı Fluxbox
kurulumları) ekstra bir sistem paketi isteyebilir:
```bash
sudo apt-get install python3-gi gir1.2-appindicator3-0.1
```

Tray'i tamamen kapatmak istersen: `--no-tray`

**Windows Güvenlik Duvarı — artık otomatik:** exe, ilk çalıştırmada kendi
kalıcı inbound UDP/TCP iznini kendisi ekler (kural zaten varsa hiçbir şey
yapmaz, tekrar sormaz). Yönetici değilsen tek bir UAC ("Bu uygulamanın
bilgisayarında değişiklik yapmasına izin ver misin?") penceresi çıkar,
onaylaman yeterli — ayrı bir `.bat`'ı elle "Yönetici olarak çalıştır"
etmene gerek yok. UAC'yi reddedersen ya da bir sebeple otomatik ekleme
başarısız olursa, yedek olarak `add_firewall_rules.bat`'ı exe'lerle aynı
klasöre koyup **"Yönetici olarak çalıştır"** ile bir kere çalıştırabilirsin.

## Otomatik Mobil Hotspot (host tasinabilir oldugu icin)

Host her calistiginda, hangi laptop olursa olsun, Windows Mobil Hotspot'unu
hep ayni isimle ("wfas wifi", parola: "wfasaudio1") acmayi dener. Client
tarafi (wfas_native_client.py) bu SSID'yi arka planda arayip otomatik
baglanir - boylece okuldaki ayri bir AP'ye (TP-Link vb.) bagimli kalinmaz,
host'un kendi WiFi'si tek hop'ta client'a dogrudan yayin yapar (RF acidan
en temiz secenek).

- Ozellestirmek istersen: `wfas_host.exe --hotspot-ssid "baska isim" --hotspot-password "baskaSifre12"`
  (client tarafinda da ayni `--wifi-ssid`/`--wifi-password` ile eslesmeli)
- Kapatmak istersen (mevcut bir AG'a elle baglanip client'i `--host` ile
  yonlendirmek istiyorsan): `--no-hotspot`
- Bazi eski WiFi kartlari/surucular SoftAP'i desteklemez - bu durumda log'da
  `[hotspot] ... acilamadi` uyarisi gorursun, host normal calismaya devam
  eder; Ayarlar -> Ag ve Internet -> Mobil Hotspot'tan elle acabilirsin.

## Sorun giderme

**Client sürekli "HANDSHAKE" ile "ERROR" arasında gidip geliyor, bağlanamıyor:**

- Bu genelde iki sebepten biri:
  1. **Windows Güvenlik Duvarı.** İlk çalıştırmada Windows bir izin
     penceresi gösterebilir ("özel ağlar" ve "genel ağlar" ikisini de
     işaretle - okul ağı genelde "Genel" profilde sayılır ve varsayılan
     olarak gelen bağlantıları engeller). Pencereyi kaçırdıysan: Windows
     Güvenlik Duvarı ayarlarından `wfas_host.exe`'ye izin ver.
  2. ~~Eski sürümde: host, ilk bağlanan client'ın adresini kilitleyip başka
     hiçbir adresten gelen bağlantıyı kabul etmiyordu~~ — bu düzeltildi;
     artık yeni bir HELLO geldiğinde (Key modu açıksa doğrulandıktan sonra)
     her zaman öncekinin yerine geçiyor, "eski bağlantı asılı kaldı"
     döngüsü oluşmaz.
- Tam hata mesajını görmek için `wfas_host_debug.exe`'yi (ya da
  `wfas_host.exe --key ...` yerine konsollu sürümü) çalıştırıp client
  tarafındaki web arayüzünde (`http://localhost:8091`) "Son hata" alanına
  bak, ya da `~/wfas_host.log` / `%USERPROFILE%\wfas_host.log` dosyasına.

## Linux

Çoğu masaüstü dağıtımda `parec` zaten kurulu gelir (PulseAudio/PipeWire
araçları). Yoksa: `sudo apt-get install pulseaudio-utils`

Tray ikonu için: `pip install pystray pillow` (opsiyonel, yoksa host
otomatik konsol-only modda çalışır).

```bash
python3 wfas_host.py
python3 wfas_host.py --key gizliAnahtar   # paylaşımlı ağda önerilir
```

## Windows — hiç kurulum yapmadan (okulda)

**En pratik yol: exe'yi evde/evinde önceden hazırla, USB ile taşı.**

Evde, internetin olan bir Windows bilgisayarda (Python kurulu olmalı):

```
build_windows_exe.bat
```

Bu, `pyinstaller`, `pyaudiowpatch`, `pystray` ve `pillow`'u kurup **iki**
exe üretir:

- `dist\wfas_host.exe` — **konsolsuz**, çift tıklayınca hiçbir pencere
  açılmaz, sadece tray ikonu görünür. Günlük kullanım için bu.
- `dist\wfas_host_debug.exe` — konsollu, sorun çıkarsa canlı log görmek
  için. Sadece hata ayıklarken kullan.

İkisi de ayrıca `%USERPROFILE%\wfas_host.log` dosyasına da yazar (konsol
olsun ya da olmasın), yani konsolsuz sürümde bile bir sorun çıkarsa o
dosyaya bakabilirsin.

Bu exe'leri USB'ye kopyala, okul bilgisayarında hiçbir kurulum yapmadan
(admin hakkı da gerekmez) direkt çalıştır:

```
wfas_host.exe --key gizliAnahtar
```

## Alternatif: gömülebilir (embeddable) Python ile, exe derlemeden

Eğer PyInstaller ile önceden exe hazırlayamıyorsan:

1. python.org/downloads/windows → **"Windows embeddable package (64-bit)"**
   zip'ini indir (bu bir kurulum değildir, sadece klasöre çıkan portable
   bir Python).
2. Bir klasöre çıkar (örn. `C:\PortablePy`).
3. Aynı klasöre `get-pip.py` indirip çalıştır: `python.exe get-pip.py`
4. `python.exe -m pip install pyaudiowpatch pystray pillow`
5. `wfas_host.py`'yi aynı klasöre koy, çalıştır:
   `python.exe wfas_host.py --key gizliAnahtar`

Bu yöntem de sisteme hiçbir şey "kurmaz" (registry/Program Files'a
dokunmaz) — sadece bir klasördeki dosyaları kullanır, USB'den de
çalıştırılabilir.

## Güvenlik notu

Okul ağı gibi paylaşımlı bir ağda **`--key` kullanmanı öneririz**. Anahtar
verilmezse discovery beacon'ı ağdaki herkese görünür ve Key modu kapalıyken
her cihaz unicast handshake yapıp yayınına bağlanabilir (HELLO gönderen ilk
cihaz kabul edilir). `--key gizliAnahtar` ile başlatırsan, client tarafında
da aynı anahtar (`--key gizliAnahtar`) girilmeden bağlantı kurulamaz.

## Sınırlamalar

- Sadece Unicast (tek client) — aynı anda ikinci biri bağlanmaya çalışırsa
  `WFAS_BUSY` alır.
- Ask modu (sunucuda onay diyaloğu) yok — Off ya da Key.
- ChaCha20-Poly1305 şifreleme yok (Key modu sadece kimlik doğrular, trafiği
  şifrelemez) — `wfas_native_client.py`'deki sınırlamayla aynı.
