# WFAS Taşınabilir Host

Sistem sesini yakalayıp `wfas_native_client.py`'nin konuştuğu WFAS UDP
protokolüyle ağa yayınlayan, kurulum gerektirmeyen bir host.

## Tray ikonu

Varsayılan olarak bir sistem tepsisi (tray) ikonu da açılır — konsol/log
çıktısı bu sırada normal şekilde akmaya devam eder, birbirini engellemez.
Kırmızı nokta = bekleniyor, **sarı nokta = client kayıtlı ama 12+ saniyedir
yanıt vermiyor (watchdog)**, yeşil nokta = bağlı ve sağlıklı. Watchdog,
client'ın `/status.json`'ındaki paket sayacının gerçekten ilerleyip
ilerlemediğine periyodik olarak (3 sn'de bir) bakıyor - client sessizce
kaybolursa (elektrik kesintisi, WiFi düşmesi, process kill) host'un UDP
tarafı bunu KENDİLİĞİNDEN fark etmez (sadece açık bir "BYE" mesajıyla ya
da yeni bir client bağlanınca temizlenir), watchdog bu boşluğu kapatıyor.
Sarıya döndüğünde tray bir bildirim de gösterir.

Sağ tık menüsünde durum yazısı, "Kapat" ve **iki ayrı ses kontrolü** var:

- **"Ses Ayarla... (kaydırıcı)"** — küçük bir Tkinter penceresi açar
  (kurulu ise `sv-ttk` ile Windows 11 Fluent/WinUI3'e yakın bir görünümde
  — gerçek WinUI3 XAML/WinRT gerektirdiği için Tkinter'a gömülemez, sv-ttk
  saf Python bir yaklaşım/taklit), içinde ÜÇ kaydırıcı:
  1. **Host Kazancı** — host'un client'a göndermeden önce ses verisine
     uyguladığı yazılımsal çarpan (0-100, tamamen yerel/anlık, ağ
     gerektirmez). Tavanı client'ın kendi sistem sesi.
  2. **Client'ın Kendi Sesi** — client makinenin GERÇEK sistem ses
     seviyesi. `/volume` endpoint'ine HTTP ile uzaktan yazıyor;
     sürüklerken ~120ms debounce ile ANLIK gönderilir (bırakmayı beklemek
     gerekmez). Sadece bir client bağlıyken aktif olur; client'ın
     `pactl`/`amixer`'ı yoksa devre dışı görünür ve neden olduğunu yazar.
  3. **Gecikme / Jitter Buffer** — client'ın çalmaya başlamadan önce
     biriktirdiği tampon süresi (ms). `/latency` endpoint'ine POST -
     canlı akışı ANINDA etkilemez, bağlantı bir sonraki kurulduğunda
     (kopup tekrar bağlanınca) uygulanır; akışı ortasından kesip yeniden
     buffer'lamak kendi başına bir kesinti yaratacağı için bilerek böyle.
  Bu iki kaydırıcı `client/wfas_native_client.py`'de ilgili endpoint'lerin
  bulunduğu bir sürüm gerektirir.
  İkona tek tıklamak da (varsayılan menü öğesi) aynı pencereyi açar.
- **"Hızlı Seviye"** — host kazancı için %100/%75/%50/%25/Sessiz hazır
  seçenekler (sadece 1 numaralı kaydırıcıyı etkiler).

Client farklı bir `--web-port` ile çalıştırıldıysa host'u da
`--client-web-port <port>` ile aynı porta işaret et (varsayılan: 8091,
ikisi de).

`--key` verilmişse (Key mode), host bu iki uzaktan-kontrol isteini de
aynı anahtarla otomatik imzalıyor (client'ta `--key` AYNI olmalı) -
ayrıca bir şey yapmana gerek yok. Anahtarlar uyuşmuyorsa tray log'una
"yetkisiz" uyarısı düşer.

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

## Sanal hoparlor (host cihazdan ses cikmasin istersen)

Host, **varsayilan olarak** sanal bir cikis olusturup onu varsayilan ses
cikisi yapar:

- Host'un kendi hoparloru/kulakligindan **ses cikmaz**.
- Sistem ses duzeyi (klavye tuslari, Windows'ta gorev cubugundaki
  hoparlor ikonundan acilan panel, Linux'ta ses karistirici) artik bu
  sanal cikisi kontrol eder - yani sistem sesini kisip acmak dogrudan
  client'a giden akisin seviyesini degistirir. Windows'ta gercek bir
  donanim cikisi (fiziksel hoparlor ya da bos jack'e takili bir kulaklik)
  varsayilan oldugunda, gorev cubugu ses panelindeki normal kaydirici
  tam olarak istedigin "client sesini kisip ac" kontrolu oluyor - ekstra
  bir arayuze gerek yok (bkz. asagidaki Windows bolumu, VB-CABLE
  KULLANILMIYOR).
- Host kapatildiginda onceki varsayilan cikis otomatik geri yuklenir.

Istemiyorsan: `--no-virtual-speaker` ile kapatabilirsin, host o zaman
normal fiziksel cikistan hem kendi hem client icin ses verir.

**Linux:** `pactl` (pulseaudio-utils / pipewire-pulse) gerektirir, coğu
dagitimda zaten kurulu. Yuklenemezse host uyari basip normal cikisla
devam eder, hataya dusmez.

**Windows:** Burada otomatik bir sanal hoparlör YOK (VB-CABLE denendi,
kaldırıldı — bkz. aşağıdaki not). Host'un kendi hoparlöründen ses
çıkmasını engellemek istiyorsan **donanımsal** bir yol kullan:

1. Host makinede kullanılmayan bir ses çıkışı bul (boş kulaklık jack'i en
   pratiği — hoparlörü kırık/patlak eski bir kulaklık bile olur, amfiye
   bağlı olması gerekmez).
2. O kulaklığı jack'e tak.
3. Görev çubuğundaki hoparlör ikonuna sağ tık → çıkış aygıtı listesinden
   **"Kulaklıklar"**ı (ya da neyse o) varsayılan seç.
4. `wfas_host.exe`'yi başlat (varsayılan olarak `--no-virtual-speaker`
   GEREKMEZ, Windows'ta bu bayrak zaten hiçbir şeyi değiştirmiyor —
   host o an sistemde varsayılan olan çıkışı kullanıyor).

Bu şekilde Windows sesi gerçek bir donanım endpoint'ine render ediyor
(görev çubuğu paneli, klavye ses tuşları hepsi normal çalışır ve
client'a giden akışı gerçekten değiştirir), ama fiziksel olarak hiçbir
yerden duyulabilir ses çıkmıyor — jack'in ucu boşta.

**Neden VB-CABLE değil:** VB-Audio'nun kendi geliştiricisi, VB-CABLE'ın
gerçek bir kazanç/gain kontrolü uygulamadığını doğruluyor — "CABLE
Input" için gösterilen ses kaydırıcısı sadece görsel, sesin seviyesine
hiçbir etkisi yok (sabit/unity gain). Yani VB-CABLE ile host'u
susturabilirsin ama Windows ses panelinden client'a giden sesi kıp
açamazsın — tam da bizim ihtiyacımızın tersi. Ayrıca kurulumu (installer
sarmalayıcısı sessiz kurulum parametrelerini iç setup'a forward etmiyor,
bazı sistemlerde restart gerekiyor) bu projenin "kurulumsuz, aç-kullan"
hedefiyle de çelişiyordu.

Tek dikkat edilecek nokta: `WindowsCapture` yakalamayı host başlarken
hangi aygıt varsayılansa ona sabitliyor; host ÇALIŞIRKEN çıkış aygıtını
panelden değiştirirsen client'a giden akış sessiz kalır (host yine de
çökmez) - geri eski aygıta alman ya da host'u yeniden başlatman yeterli.

**Not (firewall self-heal):** Eski bir build'den kalma, artık geçersiz bir
`.exe` yoluna bağlı firewall kuralı varsa (ör. daha önce farklı bir dosyadan
derlenmiş bir sürüm çalıştırdıysan), host artık bunu algılayıp otomatik
silip güncelliyor - eskiden isim eşleşmesi yeterli sayılıp bu fark edilmiyor,
Windows'un "İzin ver" penceresi hiç çıkmıyordu.

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
     olarak gelen bağlantıları engeller). Pencereyi kaçırdiysan: Windows
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

**Client bağlı ama ses yok / paket sayısı düşük:**

- Log'da (`%USERPROFILE%\wfas_host.log`) `[capture] cihaz: ... | N kanal -> client'a 2
  kanal` satırına bak. Çıkış 6/8 kanal ya da 96/192 kHz olabilir; host çok
  kanalı stereoya indirir ve paketleri 1400 bayta böler, böylece client'ın
  4096 baytlık okuma tamponuna sığar.
- `[akış] son 15 sn: 0 ses parçası yakalandı` uyarısı: WASAPI loopback
  sessizlikte veri vermez, host'ta ses çalıyor mu ve varsayılan çıkış aygıtı
  host çalışırken değiştirilmiş mi kontrol et.
- Firewall: Windows'un ilk çalıştırmada kendiliğinden açtığı `wfas_host.exe`
  adlı bir ENGELLE kuralı varsa host bunu silip kendi izin kurallarını ekler
  (UAC penceresi çıkar; log'da `[firewall]` satırları görünür).

## Linux

Çoğu masaüstü dağıtımda `parec` zaten kurulu gelir (PulseAudio/PipeWire
aracları). Yoksa: `sudo apt-get install pulseaudio-utils`

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
