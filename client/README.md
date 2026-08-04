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

## Web arayüzü

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
