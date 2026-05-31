# Raspberry Pi'de ATM Projesini Çalıştırma

Bu rehber, **graduation_project** (3 katmanlı ATM uygulaması) projesini Raspberry Pi üzerinde çalıştırmak için adım adım talimatları içerir.

## Mimari hatırlatma

```
Katman 1 — customer_app.py (Flask UI)          → Pi'de (kiosk)
Katman 2 — atm-middleware/middleware.py        → Pi veya sunucu
Katman 3 — core-banking-system (Spring Boot)   → Pi veya sunucu
```

---

## Önce karar verin: Pi ne yapacak?

| Senaryo | Pi'de çalışan | Başka makinede çalışan | Önerilen |
|---------|---------------|------------------------|----------|
| **A — Tam kiosk (önerilen)** | Katman 1 (UI) + Caddy + parmak izi | Katman 2 (middleware) + Katman 3 (Core Banking) | Pi 4/5, 2–4 GB RAM yeterli |
| **B — Hepsi Pi'de** | Katman 1 + 2 + 3 (Flask, FastAPI, Spring Boot, PostgreSQL) | — | Pi 5, **8 GB RAM** şart; yavaş olabilir |

Çoğu mezuniyet projesi / gerçek ATM kurulumu için **Senaryo A** daha mantıklıdır.

---

## Adım 1 — Donanım ve işletim sistemi

1. **Raspberry Pi 4 veya 5** (tercihen 4 GB+ RAM)
2. Raspberry Pi OS **64-bit** (Bookworm) kurun
3. USB **parmak izi sensörü** varsa takın (genelde `/dev/ttyUSB0` veya `/dev/ttyUSB1`)
4. Pi'yi ağa bağlayın (Ethernet tercih edilir)

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

---

## Adım 2 — Temel yazılımları kurun

```bash
# Python ve araçlar
sudo apt install -y python3 python3-venv python3-pip git \
  libsecret-1-0 libsecret-1-dev gnome-keyring \
  chromium-browser unclutter

# Caddy (ARM destekli)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

# mkcert (yerel TLS sertifikaları)
sudo apt install -y libnss3-tools
curl -JLO "https://dl.filippo.io/mkcert/latest?for=linux/arm64"   # Pi 32-bit ise arm
chmod +x mkcert-v*-linux-arm64
sudo mv mkcert-v*-linux-arm64 /usr/local/bin/mkcert
mkcert -install
```

**Senaryo B** (tüm stack Pi'de) için ek olarak:

```bash
# Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Oturumu kapatıp açın

# Java 21 (Core Banking için)
sudo apt install -y openjdk-21-jdk maven
```

---

## Adım 3 — Projeyi Pi'ye kopyalayın

```bash
cd ~
git clone <repo-url> graduation_project
cd graduation_project

python3 -m venv atm_venv
source atm_venv/bin/activate
pip install -r requirements.txt
```

**Senaryo A** için `core-banking-system` repoyu Pi'ye değil, sunucuya kurun. Pi sadece `graduation_project` içindeki UI katmanını çalıştırır.

---

## Adım 4 — Host adlarını ayarlayın

```bash
sudo nano /etc/hosts
```

Şunu ekleyin:

```
127.0.0.1 atm.local admin.local mw.local api.local
```

**Senaryo A**'da middleware ve Core Banking başka makinedeyse, o makinenin IP'sini de ekleyin veya `.env` içinde URL'leri o IP'ye yönlendirin.

---

## Adım 5 — `.env` dosyasını oluşturun

```bash
cp .env.example .env
nano .env
```

### Senaryo A (sadece kiosk UI) — backend uzaktaysa

```env
MIDDLEWARE_URL=https://<SUNUCU_IP>:8443   # veya mw.local (hosts ile)
FINGERPRINT_PORT=/dev/ttyUSB0             # ls /dev/ttyUSB* ile kontrol
FINGERPRINT_BAUD=9600
BIND_HOST=127.0.0.1
PORT=5001
```

Sensör yoksa (test):

```env
FINGERPRINT_SIMULATE=1
```

### Senaryo B (hepsi Pi'de)

`.env.example` varsayılanlarını kullanın:

```env
MIDDLEWARE_URL=https://mw.local
CORE_BANKING_URL=https://api.local
```

---

## Adım 6 — Gizli anahtarları keychain'e kaydedin

Hassas değerler `.env`'e **yazılmaz**; OS keychain kullanılır:

```bash
source atm_venv/bin/activate

python3 scripts/manage_secrets.py set FLASK_SECRET_KEY
python3 scripts/manage_secrets.py set MIDDLEWARE_SERVICE_TOKEN
```

**Senaryo B**'de middleware de Pi'deyse ek olarak:

```bash
python3 scripts/manage_secrets.py set CONTRACT_ADDRESS
python3 scripts/manage_secrets.py set ETH_PRIVATE_KEY
python3 scripts/manage_secrets.py set MIDDLEWARE_DB_URL
```

`MIDDLEWARE_SERVICE_TOKEN` değeri, Core Banking tarafındaki ile **aynı** olmalı.

---

## Adım 7 — TLS ve mTLS sertifikalarını oluşturun

```bash
cd ~/graduation_project

# Caddy yapılandırması + sunucu sertifikaları
./scripts/caddy/install_caddyfile.sh

# Kiosk → middleware mTLS istemci sertifikası
./scripts/gen_kiosk_client_cert.sh
```

**Senaryo B**'de ek olarak:

```bash
./scripts/gen_mtls_client_cert.sh      # middleware → Core Banking
./scripts/gen_admin_client_cert.sh     # admin panel (opsiyonel)
./scripts/gen_postgres_server_cert.sh    # PostgreSQL TLS
```

---

## Adım 8 — Core Banking (Katman 3) — Senaryo B

`core-banking-system` reposunda (Pi'de veya sunucuda):

```bash
cd core-banking-system
docker compose up -d          # PostgreSQL :5332 ve :5433
./mvnw spring-boot:run        # :8080
```

`MIDDLEWARE_SERVICE_TOKEN` ortam değişkenini Spring Boot tarafında da ayarlayın.

---

## Adım 9 — Middleware (Katman 2) — Senaryo B

```bash
source ~/graduation_project/atm_venv/bin/activate
cd ~/graduation_project/atm-middleware
python3 middleware.py         # :8000
```

---

## Adım 10 — Caddy'yi başlatın

```bash
cd ~/atm-tls
sudo caddy run --config Caddyfile
```

Arka planda çalışsın isterseniz:

```bash
sudo caddy start --config ~/atm-tls/Caddyfile
```

---

## Adım 11 — ATM arayüzünü (Katman 1) başlatın

```bash
source ~/graduation_project/atm_venv/bin/activate
cd ~/graduation_project
python3 customer_app.py       # varsayılan :5001
```

Tarayıcıda açın: **https://atm.local**

(`http://127.0.0.1:5001` değil — Caddy üzerinden gitmeli)

Admin panel gerekiyorsa ayrı terminalde:

```bash
python3 admin-app/admin_app.py   # :5002 → https://admin.local
```

---

## Adım 12 — Parmak izi sensörünü kontrol edin

```bash
ls -l /dev/ttyUSB*
groups $USER    # dialout grubunda olmalı
sudo usermod -aG dialout $USER
```

Portu `.env`'de doğru ayarlayın:

```env
FINGERPRINT_PORT=/dev/ttyUSB0
```

---

## Adım 13 — Kiosk modu (tam ekran ATM)

Otomatik giriş + tam ekran Chromium:

```bash
mkdir -p ~/.config/autostart
nano ~/.config/autostart/atm-kiosk.desktop
```

İçerik:

```ini
[Desktop Entry]
Type=Application
Name=ATM Kiosk
Exec=chromium-browser --kiosk --noerrdialogs --disable-infobars https://atm.local
X-GNOME-Autostart-enabled=true
```

Fare imlecini gizlemek için:

```bash
unclutter -idle 1 &
```

---

## Adım 14 — Başlatma sırası (özet)

### Senaryo B (hepsi Pi'de)

```
1. Docker (PostgreSQL)
2. Spring Boot (Core Banking)     → :8080
3. Middleware                     → :8000
4. Caddy                          → TLS proxy
5. customer_app.py                → :5001
6. (Opsiyonel) admin_app.py       → :5002
7. Chromium kiosk → https://atm.local
```

### Senaryo A (sadece kiosk)

```
1. Sunucuda: PostgreSQL + Spring Boot + Middleware + Caddy
2. Pi'de: Caddy (veya doğrudan sunucuya bağlan) + customer_app.py
3. Chromium kiosk
```

---

## Sık karşılaşılan sorunlar

| Sorun | Çözüm |
|-------|-------|
| `Kiosk mTLS client cert missing` | `./scripts/gen_kiosk_client_cert.sh` çalıştırın |
| `Worker running but chain reconciliation inactive` | Keychain'de `CONTRACT_ADDRESS` ve `ETH_PRIVATE_KEY` ayarlayın |
| Parmak izi bağlanmıyor | `FINGERPRINT_PORT`, `dialout` grubu, `FINGERPRINT_SIMULATE=1` ile test |
| Keychain çalışmıyor (headless Pi) | `gnome-keyring` kurulu olsun; oturum açık kalsın |
| Pi çok yavaş | Core Banking + middleware'i ayrı sunucuya taşıyın (Senaryo A) |

---

## Minimum test kontrol listesi

1. `https://atm.local` açılıyor mu?
2. Kart numarası + PIN ile giriş yapılabiliyor mu?
3. Para yatır/çek çalışıyor mu?
4. Parmak izi kaydı (varsa) tamamlanıyor mu?
5. İşlem sonrası Etherscan QR kodu görünüyor mu?

---

## İlgili dosyalar

- Ana kurulum: [`README.md`](../README.md)
- Ortam değişkenleri: [`.env.example`](../.env.example)
- Parmak izi entegrasyonu: [`CORE_BANKING_FINGERPRINT.md`](CORE_BANKING_FINGERPRINT.md)
- Caddy yapılandırması: [`scripts/caddy/install_caddyfile.sh`](../scripts/caddy/install_caddyfile.sh)
