# WFAS Native UDP Client

WiFi Audio Streaming'in native WFAS UDP protokolünü (raw 16-bit PCM, unicast)
doğrudan konuşan hafif bir client. Chromium/Playwright gerektirmez; ses
doğrudan `aplay` (ALSA) üzerinden çalınır. Amaç: en düşük gecikme, en az
kaynak kullanımı, tek cihaza (Unicast) bağlanma.

## Gereken sunucu ayarları

1. **Ayarlar -> "Advertise HTTP stream" KAPALI.** Açıkken uygulama
   Multicast'i zorunlu kılıyor ve Unicast anahtarı gri/kilitli görünüyor.
2. **Multicast anahtarı KAPALI**, yani sunucu Unicast modunda olmalı.

Bu iki ayar olmadan bu client bağlanamaz (bağlanmaya çalışır ama sunucunun
Multicast modunda olduğunu tespit edip ekranda/web arayüzünde uyarı basar).

## Kurulum

```bash
chmod +x install.sh
./install.sh
```

Script `alsa-utils` (aplay için) kurar, dosyaları `~/wfas-client-native/`
altına kopyalar ve bir systemd user servisi hazırlar.

## Elle test

```bash
python3 wfas_native_client.py
```

Sunucu otomatik keşfedilir (aynı ağdaki multicast discovery beacon'ı
dinlenir). Otomatik bulunamazsa:

```bash
python3 wfas_native_client.py --host 192.168.1.50 --port 5000
```

(`--port`, uygulamanın WFAS streaming portu — discovery beacon'daki
`<port>` alanı; HTTP portuyla karıştırma.)

Sunucuda "Key" güvenlik modu açıksa:

```bash
python3 wfas_native_client.py --key gizliAnahtar
```

## Otomatik WiFi bağlanma (host'un Mobil Hotspot'una)

Host tasinabilir oldugu ve her seferinde farkli bir laptop olabildigi icin,
host artik kendi Mobil Hotspot'unu hep ayni SSID ile ("wfas wifi", parola:
"wfasaudio1") aciyor. Client bu SSID'yi arka planda periyodik olarak tarar
(nmcli/NetworkManager gerekir) ve gorunce otomatik baglanir - hangi laptop
host olursa olsun elle WiFi ayarlarina girmek gerekmez.

- Ozellestirilmisse (host'ta `--hotspot-ssid`/`--hotspot-password`
  kullanildiysa) client tarafinda da eslesmesi gerekir:
  `python3 wfas_native_client.py --wifi-ssid "baska isim" --wifi-password "baskaSifre12"`
- Kapatmak istersen (mevcut agi elle yonetmek istiyorsan): `--no-wifi-autoconnect`
- `nmcli` yoksa (NetworkManager kullanilmiyorsa) bu ozellik sessizce devre
  disi kalir, geri kalan her sey (discovery, --host ile manuel baglanma)
  normal calismaya devam eder.

## Ses seviyesi (client'ın kendi sistem sesi) ve gecikme (jitter buffer)

Bu, host'un veriye uyguladığı yazılımsal kazanç (host tarafındaki tray
kaydırıcısı) ile KARIŞTIRILMAMALI: host gain'i sadece kaynağı aşağı kısar,
client'ın kendi sistem sesi ise gerçek tavanı belirler. İkisi birlikte
kullanılır.

Client açılışta hangisi kuruluysa onu otomatik seçer:
- **PulseAudio/PipeWire** varsa `pactl` (`pulseaudio-utils` paketi).
- Yoksa çıplak **ALSA** `amixer` (`alsa-utils` paketi - zaten `aplay` için
  kurulu).

İkisi de yoksa client normal çalışmaya devam eder, sadece ses seviyesi
okunamaz/değiştirilemez (log'da tek satır uyarı basar).

Kontrol iki yerden yapılabilir:
- **Web arayüzü** (`http://<client-ip>:8091`) — durum kartının altındaki
  ses ve gecikme kaydırıcıları.
- **HTTP API** — LAN üzerinden herhangi bir yerden (ör. host'un kendisinden,
  host zaten bağlı client'ın IP'sini biliyor):
  ```bash
  curl -X POST http://<client-ip>:8091/volume \
       -H 'Content-Type: application/json' -d '{"value": 60}'
  curl -X POST http://<client-ip>:8091/latency \
       -H 'Content-Type: application/json' -d '{"value": 200}'
  ```
  `status.json` yanıtına `volume` (0-100), `volume_backend`
  (`"pactl"`/`"amixer"`/`null`) ve `prebuffer_ms` alanları eklendi.

**Gecikme/jitter buffer** (`--prebuffer-ms`, varsayılan 120): client
çalmaya başlamadan önce ne kadar ses biriktirsin. Küçültürsen host'ta bir
medyayı durdurunca hissedilen gecikme azalır ama ağdaki ufak sapmalara
karşı payın da azalır (ses kesilmesi riski artar); büyütürsen tam tersi.
`/latency` ile gönderilen değer AKAN bir oturumu anında etkilemez -
bağlantı bir sonraki kurulduğunda (kopup tekrar bağlanınca) uygulanır.

## /volume ve /latency için kimlik doğrulama

`--key` verilmişse (Key mode), aynı anahtar bu iki endpoint'i de korur -
HMAC-SHA256 imzalı `X-WFAS-Ts`/`X-WFAS-Auth` header'ları gerekir (imza
±30 sn içinde olmalı, replay koruması için). `--key` VERİLMEDİYSE (Open
mode) bu endpoint'ler de açık kalır - ses akışıyla tutarlı davranış.

Önemli: Key mode açıkken **web arayüzündeki kaydırıcılar çalışmaz**
(401 döner, sayfa bunu bir uyarıyla gösterir) - bilerek böyle, anahtarı
tarayıcıya/HTML'e gömmek onu herkese açık hale getirirdi. Key mode
açıkken kontrol host tray'inden (otomatik imzalıyor) ya da elle imzalı
bir `curl` isteğiyle yapılmalı:
```bash
KEY=gizliAnahtar; TS=$(date +%s); BODY='{"value": 60}'
SIG=$(printf '%s:%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$KEY" | awk '{print $2}')
curl -X POST http://<client-ip>:8091/volume -H "Content-Type: application/json" \
     -H "X-WFAS-Ts: $TS" -H "X-WFAS-Auth: $SIG" -d "$BODY"
```



`http://localhost:8091` — bağlantı durumu, sunucu, format (Hz/kanal),
alınan paket sayısı, kayıp nedeniyle sessizlikle doldurulan örnek sayısı ve
son hatayı gösterir. Aynı sayfadan IP/port girip "Bağlan" ile manuel olarak
başka bir sunucuya da geçilebilir.

## Açılışta otomatik başlatma

```bash
systemctl --user enable --now wfas-native-client.service
journalctl --user -u wfas-native-client -f
```

## Sınırlamalar

- Sadece Unicast desteklenir (Multicast dinleme yok).
- Bölüm 8'deki ChaCha20-Poly1305 şifrelemesi uygulanmadı — Key modu sadece
  kimlik doğrulama sağlar, trafiği şifrelemez. Ev ağında güvenlik kapalıysa
  (varsayılan) bu bir sorun değil.
- Kayıp/sıra dışı paketler basit bir "sample position'a göre sessizlik
  ekleme" ile telafi edilir; gerçek bir jitter buffer/yeniden sıralama
  kuyruğu yoktur. Ev ağı için genelde yeterlidir.

## AppImage (kurulumsuz tek dosya)

Python dahil her şeyi içeren tek dosya: `WFAS-Client-x86_64.AppImage`. GitHub'da
**Actions → Build client (AppImage)** çalışmasının artifact'ından (ya da `v*`
tag'i atılırsa Releases'ten) indirilir. Yerelde derlemek için:
`client/appimage/build-appimage.sh` (çıktı: `client/appimage/dist/`).

Gereken sistem paketleri aynı: `aplay` için `alsa-utils`, ses seviyesi için
`pactl` ya da `amixer`, WiFi otomatik bağlanma için `nmcli` (opsiyonel).
`libfuse2` yoksa AppImage kendiliğinden çalışmaz; `APPIMAGE_EXTRACT_AND_RUN=1`
ile çalıştırılır (kurulum komutları bunu servise otomatik ekler).

```bash
chmod +x WFAS-Client-x86_64.AppImage
./WFAS-Client-x86_64.AppImage                        # istemci, otomatik keşif
./WFAS-Client-x86_64.AppImage --key gizliAnahtar     # tüm istemci argümanları aynen geçer
./WFAS-Client-x86_64.AppImage --tui                  # durum ekranı
```

**Açılışta otomatik başlatma:**

```bash
# systemd --user varsa (önerilir): dosyayı ~/.local/bin'e kopyalar, servisi kurar ve başlatır
./WFAS-Client-x86_64.AppImage --install-service --key gizliAnahtar
journalctl --user -u wfas-native-client -f

# systemd yoksa (SysVinit + Fluxbox): ~/.fluxbox/startup'a durum penceresi ekler
./WFAS-Client-x86_64.AppImage --install-autostart --key gizliAnahtar

# ikisini de kaldırır
./WFAS-Client-x86_64.AppImage --uninstall
```

`--install-service` / `--install-autostart`'tan sonra verilen argümanlar
(`--key`, `--host`, `--port` ...) servise aynen yazılır. Fluxbox yönteminde
istemci durum penceresinin içinde çalışır; pencere kapanırsa istemci de durur.
