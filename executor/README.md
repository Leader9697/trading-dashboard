# Eksekutor order otomatis (engine v3)

Program ini menjalankan engine portofolio v3 dari dashboard sebagai order Binance Futures (USDT-M), persis seperti backtest-nya. Programnya jalan 24 jam di VPS.

Cara kerjanya:
- Program mengecek setiap 30 detik. Dalam 30 menit setelah candle 4 jam tutup, program membaca engine dari dashboard: daftar koin (sekarang 20), risiko per trade (sekarang 0,75%), maksimal posisi (sekarang 8), dan sinyal tiap koin. Angka ini ikut engine di dashboard, jadi kalau engine diperbarui, program ikut tanpa perlu diubah.
- Sinyal aktif dipasang sebagai **order limit** di harga entry sinyal, mulai dari skor terkuat. Kalau tidak terisi dalam 16 jam, order dibatalkan.
- Begitu terisi, langsung dipasang stop loss dan take profit (4R) yang menutup seluruh posisi, dipicu oleh mark price. Kalau stop loss gagal dipasang, posisi langsung ditutup.
- Posisi yang masih terbuka 5 hari setelah terisi ditutup di harga pasar (time exit).
- Maksimal 1 posisi per koin dan 8 posisi + order limit sekaligus. Ukuran posisi dihitung supaya kalau kena stop loss, ruginya 0,75% saldo. Margin isolated dengan leverage di bawah 1/(2 × SL%). Koin yang ordernya di bawah minimal Binance dilewati.
- Setiap order, isi, pembatalan, penutupan, sinyal yang dilewati, dan error dikirim ke Telegram.
- Pengaman (tidak mengubah aturan trading, jadi hasilnya tetap seperti backtest):
  - Tiap 5 menit dicek apakah setiap posisi masih punya SL dan TP di Binance. Kalau ada yang hilang (misalnya terhapus tidak sengaja), dipasang lagi dan dilaporkan.
  - Kalau jawaban Binance hilang saat memasang order (internet putus sesaat), order itu tetap diawasi: kalau ternyata terisi, SL/TP tetap dipasang; kalau tidak, dibatalkan setelah 16 jam.
  - Posisi di Binance yang tidak diurus program (dibuka manual) dilaporkan sekali ke Telegram.
  - Kalau data engine di dashboard tidak bisa dibaca dalam 30 menit setelah candle tutup, ada peringatan.
  - Setiap hari jam 07.00 WIB ada pesan 💓 (saldo, posisi, order menunggu). **Kalau pagi tidak ada pesan ini, berarti VPS atau program mati.** Setiap tanggal 1 ada 📊 hasil bulan lalu setelah fee dan funding.
  - Rem darurat manual: `touch PAUSE` di folder executor menghentikan entry baru (posisi yang ada tetap diurus), `rm PAUSE` menyalakan lagi. Opsional `MIN_BALANCE` di `.env`: entry baru berhenti kalau saldo di bawah angka itu. Defaultnya mati.

Defaultnya `DRY_RUN=1`, artinya hanya simulasi dan tidak ada order sungguhan.

## 1. Masuk ke VPS

Dari email SocialVPS, catat **IP**, **username** (biasanya `root`) dan **password**. Di PC, buka PowerShell:

```
ssh root@IP_VPS
```

Ketik `yes` kalau ditanya, lalu masukkan password (tidak kelihatan saat diketik, itu normal).

## 2. Buat API key Binance

Binance → Profil → API Management → Create API → System generated.

- Centang **Enable Futures**. **Enable Withdrawals harus tetap mati.**
- Pilih **Restrict access to trusted IPs only**, lalu isi IP VPS tadi.
- Di pengaturan Futures, pastikan mode posisinya **One-way** (bukan Hedge).
- Pindahkan saldo USDT ke dompet **USDⓈ-M Futures**.
- Simpan API key dan secret hanya di VPS. **Jangan kirim ke chat siapa pun**, termasuk ke Claude.

## 3. Pasang program di VPS

Salin baris per baris:

```
apt update && apt install -y git python3 python3-pytest
timedatectl set-ntp true
git clone https://github.com/Leader9697/trading-dashboard.git
cd trading-dashboard/executor
cp .env.example .env
nano .env
```

Di `nano`, isi `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` (sama dengan yang di Vercel). Simpan dengan Ctrl+O, Enter, lalu keluar dengan Ctrl+X. Lalu:

```
chmod 600 .env
python3 executor.py check
```

`check` mengecek jam VPS, saldo, mode posisi, izin API key (withdraw harus mati, IP harus dikunci), dan engine di dashboard, lalu mengirim pesan ke Telegram. Kalau semuanya benar, pasang sebagai layanan yang jalan terus dan hidup lagi sendiri setelah VPS restart:

```
cat > /etc/systemd/system/executor-v3.service <<EOF
[Unit]
Description=Eksekutor v3
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$PWD
ExecStart=/usr/bin/python3 $PWD/executor.py loop
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now executor-v3
```

Lalu pasang update otomatis. Setiap hari jam 05.30 WIB program mengambil versi terbaru dari GitHub, menjalankan tesnya, dan baru restart kalau tesnya lulus. Jadi perbaikan dari Claude sampai ke VPS tanpa perlu login:

```
cat > /etc/systemd/system/executor-v3-update.service <<EOF
[Service]
Type=oneshot
ExecStart=/bin/sh $PWD/update.sh
EOF
cat > /etc/systemd/system/executor-v3-update.timer <<EOF
[Timer]
OnCalendar=*-*-* 22:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now executor-v3-update.timer
```

## 4. Simulasi dulu, lalu uang asli

1. Biarkan `DRY_RUN=1` minimal 1–2 hari. Setiap sinyal muncul di Telegram sebagai "🧪 SIMULASI" lengkap dengan harga limit, SL, TP, jumlah koin dan risikonya. Cocokkan dengan panel "Engine portofolio" di dashboard.
2. Kalau sudah yakin, buka `nano .env`, ubah ke `DRY_RUN=0`, simpan, lalu `systemctl restart executor-v3`. Telegram akan bilang "ORDER SUNGGUHAN".

Opsional: uji siklus penuh dengan uang palsu di testnet (https://testnet.binancefuture.com, buat API key di sana), isi `BINANCE_TESTNET=1` dan `DRY_RUN=0`. Harga testnet berbeda dari pasar asli, jadi testnet hanya untuk melihat order, SL/TP dan pembatalan berjalan.

## Perintah sehari-hari

| Perlu apa | Perintah |
|---|---|
| Lihat order/posisi yang diurus | `cd ~/trading-dashboard/executor && python3 executor.py status` |
| Lihat log | `journalctl -u executor-v3 -n 50` |
| Hentikan program | `systemctl stop executor-v3` |
| Jalankan lagi | `systemctl start executor-v3` |
| Perbarui program sekarang (biasanya otomatis tiap pagi) | `sh ~/trading-dashboard/executor/update.sh` |
| Hentikan entry baru saja | `touch ~/trading-dashboard/executor/PAUSE` (hapus file itu untuk lanjut) |

Menghentikan program tidak menutup posisi. SL dan TP yang sudah terpasang tetap ada di Binance, tapi order limit yang belum terisi, pemasangan SL/TP setelah terisi, dan time exit hanya jalan selama program hidup. Untuk berhenti total: hentikan program, tutup posisi dan order di aplikasi Binance, lalu hapus API key-nya.

## Supaya Claude bisa ikut mengelola VPS

Sekali saja, di PowerShell PC (ganti `IP_VPS` di dua tempat):

```
ssh-keygen -t ed25519
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@IP_VPS "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
Add-Content $env:USERPROFILE\.ssh\config "`nHost vps`n  HostName IP_VPS`n  User root"
ssh vps "systemctl is-active executor-v3"
```

Tekan Enter saja kalau `ssh-keygen` bertanya. Kalau baris terakhir menjawab `active` tanpa minta password, Claude bisa membuka sesi Remote Control di PC dan menjalankan `ssh vps ...` untuk cek log, status dan hasil. File `.env` (API key) tidak pernah dibaca.

## Tes

```
python3 -m pytest -q executor/test_executor.py
```
