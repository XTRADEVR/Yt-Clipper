# AUTOCLIP

Website clipper otomatis: tempel link YouTube, sistem mengunduh video, memotongnya
menjadi klip pendek (15-60 detik), lalu menampilkan hasilnya untuk diunduh satu per satu.

## Fitur

- Input URL YouTube dengan validasi (di browser dan di server)
- Unduh video dengan yt-dlp, potong dengan FFmpeg
- Pilihan durasi klip: 15, 30, 45, atau 60 detik
- Bilah progres real-time, pratinjau video, dan tombol unduh per klip
- Penanganan error: URL tidak valid, video private/tidak tersedia, FFmpeg belum terpasang, durasi melebihi batas
- Hasil dihapus otomatis setelah 1 jam

## Prasyarat

- Python 3.9 atau lebih baru
- **FFmpeg** (wajib, harus bisa dipanggil dari terminal dengan perintah `ffmpeg`)

Pasang FFmpeg:

| Sistem | Perintah |
|--------|----------|
| Windows | `winget install Gyan.FFmpeg` (lalu buka terminal baru) |
| macOS | `brew install ffmpeg` |
| Ubuntu/Debian | `sudo apt update && sudo apt install ffmpeg` |
| Termux (Android) | `pkg install ffmpeg python` |

Cek pemasangan: `ffmpeg -version`

## Instalasi

```bash
git clone https://github.com/USERNAME_ANDA/autoclip.git
cd autoclip

python -m venv venv
# Windows:      venv\Scripts\activate
# macOS/Linux:  source venv/bin/activate

pip install -r requirements.txt
```

(Ganti `USERNAME_ANDA` dengan username GitHub Anda jika menyalin dari repo sendiri.)

## Menjalankan

```bash
python app.py
```

Buka http://127.0.0.1:5000 di browser.

## Konfigurasi (opsional, lewat environment variable)

| Variabel | Default | Fungsi |
|----------|---------|--------|
| `AUTOCLIP_MAX_VIDEO_SECONDS` | `1800` | Batas durasi video (detik) |
| `AUTOCLIP_MAX_CLIPS` | `12` | Maksimal klip per video (2-99) |
| `HOST` | `127.0.0.1` | Alamat server |
| `PORT` | `5000` | Port server |
| `FLASK_DEBUG` | (kosong) | Isi `1` untuk mode debug |

Contoh (Linux/macOS): `AUTOCLIP_MAX_VIDEO_SECONDS=3600 python app.py`

## Struktur folder

```
autoclip/
├── app.py
├── requirements.txt
├── README.md
├── LICENSE
├── .gitignore
├── downloads/
│   └── .gitkeep
├── templates/
│   └── index.html
└── static/
    ├── css/
    │   └── style.css
    └── js/
        └── app.js
```

## Cara kerja

1. Browser mengirim URL ke `POST /api/process`; server memvalidasi dan membuat job.
2. Thread latar belakang: cek metadata (durasi, ketersediaan), unduh video maks 720p, potong dengan FFmpeg.
3. Browser memanggil `GET /api/status/<job_id>` tiap 1,5 detik untuk progres.
4. Setelah selesai, klip tampil dengan pratinjau dan tombol unduh (`GET /files/<job_id>/<nama>?dl=1`).

Jika video menghasilkan lebih dari `AUTOCLIP_MAX_CLIPS` klip, klip dipilih merata di sepanjang video.

## Pemecahan masalah

- **"FFmpeg belum terpasang"**: pasang FFmpeg lalu pastikan `ffmpeg -version` jalan di terminal yang sama.
- **Gagal mengunduh / error anti-bot**: perbarui yt-dlp dengan `pip install -U yt-dlp` (YouTube sering berubah).
- **Video dibatasi usia atau khusus anggota**: tidak didukung.
- **Proses lambat**: pemotongan memakai re-encode agar akurat; durasi bergantung CPU.

## Catatan hukum

Proyek ini untuk keperluan edukasi dan penggunaan pribadi. Mengunduh dan memotong konten YouTube
**harus sesuai hak cipta dan [Ketentuan Layanan YouTube](https://www.youtube.com/t/terms)**.
Gunakan hanya untuk video milik Anda, berlisensi terbuka (misalnya Creative Commons), atau yang
Anda punya izin tertulis dari pemiliknya. Pengguna bertanggung jawab penuh atas penggunaannya.

## Lisensi

MIT. Lihat berkas `LICENSE`.
