# WFAS — WiFi Audio Streaming (Host + Native Client)

Bir bilgisayarın sistem sesini ağ üzerinden başka bir cihaza (Linux)
düşük gecikmeyle yayınlayan, kurulum gerektirmeyen bir host/client çifti.

- **`host/`** — Windows/Linux üzerinde çalışan, sistem sesini yakalayıp
  WFAS UDP protokolüyle yayınlayan taşınabilir host. Kuruluma gerek
  duymaz, USB'den de çalıştırılabilir.
- **`client/`** — Bu protokolü konuşan, Chromium/Playwright gerektirmeyen
  hafif Linux native client. Ses doğrudan `aplay` (ALSA) üzerinden çalar.

Host ile client aynı özel protokolü konuştuğu için ikisi birlikte
geliştirilir ve versiyonlanır.

## Hızlı başlangıç

1. Host'u başlat (Windows exe ya da `python3 wfas_host.py`) — detaylar
   [`host/HOST_README.md`](host/HOST_README.md).
2. Client'ı kur ve çalıştır — detaylar
   [`client/README.md`](client/README.md).
3. Host varsayılan olarak kendi WiFi hotspot'unu açar (`wfas wifi`),
   client bunu otomatik bulup bağlanır — ayrı bir ağa bağımlı kalmadan
   tek hop'ta yayın yapılır.

## Güvenlik notu

Paylaşımlı bir ağdaysan (ör. okul ağı) host'u `--key gizliAnahtar` ile
başlat, client'ta da aynı anahtarı kullan. Bu sadece kimlik doğrular;
trafiği şifrelemez (bkz. alt README'lerdeki sınırlamalar bölümü).
