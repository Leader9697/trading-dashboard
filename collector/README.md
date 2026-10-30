# Collector data Binance futures

Aplikasi ini menyimpan funding, open interest, rasio long/short dan taker setiap 5 menit, serta setiap likuidasi secara real-time. Data disimpan ke `collector/data/` sebagai file CSV harian.

Binance hanya menyimpan sekitar 30 hari untuk data ini, dan data likuidasi hanya tersedia live. Karena itu datanya harus dikumpulkan sendiri supaya bisa di-backtest.

Aplikasi ini memakai endpoint publik saja, jadi tidak perlu API key.

```
pip install websockets
python collect.py
```

Biarkan aplikasi berjalan terus, misalnya lewat Task Scheduler di Windows atau `nohup`/systemd di Linux/Mac.
Pemakaian disk sekitar 1–3 MB per hari.
