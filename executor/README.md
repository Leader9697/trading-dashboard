# Eksekutor order otomatis

Program ini memasang sinyal v3 dari dashboard sebagai order Binance Futures (USDT-M), lengkap dengan stop loss dan take profit. Programnya jalan di server gratis Oracle Cloud yang menyala 24 jam.

Cara kerjanya:
- Tiap 5 menit program bangun. Dalam 30 menit setelah candle 4 jam tutup, program membaca sinyal tiap koin dari dashboard.
- Kalau ada sinyal 6/6, program pasang order market. Ukurannya dihitung supaya kalau kena stop loss, ruginya `RISK_PCT`% dari saldo.
- Setelah itu dipasang stop loss dan take profit (2,5R) yang menutup seluruh posisi, dengan margin isolated.
- Kalau stop loss gagal dipasang, posisi langsung ditutup, supaya tidak ada posisi tanpa SL.
- Maksimal 1 posisi per koin. Setelah `MAX_LOSSES_PER_DAY` kali rugi dalam sehari (WIB), tidak ada entry baru sampai besok.
- Order sisa dari posisi yang sudah tutup dibersihkan otomatis.
- Setiap order, entry yang dilewati, atau error dilaporkan ke Telegram.

Defaultnya `DRY_RUN=1`, artinya hanya simulasi dan tidak ada order sungguhan.

## 1. Buat server Oracle (gratis)

1. Daftar di https://signup.cloud.oracle.com. Kartu debit/kredit hanya untuk verifikasi, tidak ditagih selama memakai resource "Always Free".
2. **Home region** dipilih sekali dan tidak bisa diganti. Pilih **Indonesia North (Batam)** kalau ada. Kalau tidak ada, pilih **Japan East (Tokyo)** atau **Singapore**. Hindari region Amerika, karena Binance memblokir IP Amerika.
3. Masuk ke Compute → Instances → Create instance.
   - Image: **Ubuntu 24.04**
   - Shape: **VM.Standard.E2.1.Micro** (Always Free)
   - Di bagian SSH keys, pilih "Generate a key pair" lalu **unduh private key**-nya.
4. Setelah statusnya Running, catat **Public IP address**-nya.

## 2. Buat API key Binance

Binance → Profil → API Management → Create API → System generated.

- Centang **Enable Futures**. Biarkan **Enable Withdrawals tetap mati**.
- Pilih **Restrict access to trusted IPs only**, lalu isi Public IP server Oracle tadi.
- Di pengaturan Futures, pastikan mode posisinya **One-way** (bukan Hedge).
- Simpan API key dan secret. **Jangan kirim ke chat siapa pun**, termasuk ke Claude.

## 3. Pasang program di server

Dari PowerShell di PC:

```
ssh -i C:\path\ke\private-key.key ubuntu@IP_SERVER
```

Lalu di server:

```
git clone https://github.com/Leader9697/trading-dashboard.git
cd trading-dashboard/executor
cp .env.example .env
nano .env        # isi API key, secret, token Telegram, chat id. Simpan: Ctrl+O, Enter, Ctrl+X
chmod 600 .env
python3 executor.py check
```

`check` mengecek koneksi, saldo dan mode posisi, lalu mengirim pesan ke Telegram. Kalau semuanya sudah benar, pasang jadwalnya:

```
(crontab -l 2>/dev/null; echo "*/5 * * * * cd $HOME/trading-dashboard/executor && python3 executor.py run >> run.log 2>&1") | crontab -
```

Selama `DRY_RUN=1`, setiap sinyal hanya dilaporkan sebagai "SIMULASI" di Telegram. Kalau sudah yakin, ubah ke `DRY_RUN=0` di `.env`.

Untuk memperbarui program: `cd ~/trading-dashboard && git pull`.

Untuk berhenti total: `crontab -r`. Atau hapus API key-nya di Binance.

## Tes

```
python3 -m pytest -q executor/test_executor.py
```
