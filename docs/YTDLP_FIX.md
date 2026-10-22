# YouTube indirme düzeltmesi (yt-dlp 403) — 2026-10-22

**Sonuç: çalışıyor.** Bu PC'de, proje `.venv`'i içindeki **yt-dlp 2026.10.21 + yt-dlp-ejs 0.8.0 + deno 2.9.8**
ile, yt-dlp'nin **varsayılan player client'larıyla**, **çerezsiz** ve **PO token gerekmeden** indirme,
ytsearch ve kanal listeleme başarılı. Kod bu ayarları `.env` üzerinden kullanıyor.

## 1. Ortam kontrolü

| Araç | Sürüm / durum |
|---|---|
| Python (sistem) | 3.11.9 — `C:\Users\Emre\AppData\Local\Programs\Python\Python311\python.exe` (PATH'te **yok**) |
| Python (.venv) | 3.11.9, torch 2.3.1+cu118, CUDA görünür (2 GPU) |
| Node.js | v24.19.0 — `C:\Program Files\nodejs\node.exe` (PATH'te **yok**) |
| deno | 2.9.8 — `tools\deno\deno.exe` (portable, sisteme kurulmadı) |
| FFmpeg | 2026-08-03 git build (gyan.dev), libavfilter 12.3 (≥ FFmpeg 8) — `C:\ffmpeg\bin` (PATH'te var) |
| yt-dlp | eski: 2026.08.19 (sistem Python) → **2026.10.21** (.venv) |
| yt-dlp-ejs | 0.8.0 |
| bgutil-ytdlp-pot-provider | 2.0.2 (eklenti + `tools\bgutil-ytdlp-pot-provider\server`) |

### PATH sorunu (senin düzeltmen gerekiyor)

Sistem (Machine) PATH kayıt defterinde sadece şu üç girdi var:
`C:\realesrgan-ncnn-vulkan-20220424-windows;C:\ffmpeg\bin;C:\Program Files\Git\cmd`.
**`C:\Windows\System32`, `C:\Windows`, `Wbem`, `WindowsPowerShell\v1.0`, `OpenSSH` ve `C:\Program Files\nodejs\` eksik.**
`setx`/`git`/`python`/`nvidia-smi`/`node` "bulunamadı" hatalarının sebebi bu. Bu belirti, genellikle
`setx PATH "..."` komutunun PATH'i kesip üzerine yazmasından kaynaklanır (setx 1024 karakterde keser).
Ben sistem ayarına dokunmadım. Düzeltmek için:

1. Başlat → "ortam değişkenleri" → **Sistem ortam değişkenlerini düzenleyin** → *Ortam Değişkenleri…*
2. **Sistem değişkenleri** altında `Path` → *Düzenle* → *Yeni* ile şunları ekle (mevcutları silme):
   - `%SystemRoot%\system32`
   - `%SystemRoot%`
   - `%SystemRoot%\System32\Wbem`
   - `%SYSTEMROOT%\System32\WindowsPowerShell\v1.0\`
   - `%SYSTEMROOT%\System32\OpenSSH\`
   - `C:\Program Files\nodejs\`
   - (istersen) `C:\Users\Emre\AppData\Local\Programs\Python\Python311\` ve `...\Python311\Scripts\`
3. Tamam → oturumu kapatıp aç (ya da yeniden başlat).
4. **Bir daha `setx PATH` kullanma.** PATH'i bu pencereden düzenle.

Kod bu sorundan etkilenmesin diye sistem araçları tam yolla çağrılıyor: deno/node (`YTDLP_JS_RUNTIME_PATH`),
`ollama.exe` (`OLLAMA_EXE`, varsayılan `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`), `powershell.exe`
(`%SystemRoot%\System32\...`), yt-dlp (`.venv\Scripts\python.exe -m yt_dlp`).

## 2. setup_venv.ps1

- **1. çalıştırma:** venv, torch cu118, `requirements.txt`, yt-dlp ve bgutil eklentisi kuruldu. Ardından `npm ci`
  hata verdi: `canvas` paketinin kurulum script'i `node`'u çağırıyor, ama Node PATH'te değil
  (`'node' is not recognized`).
- **Düzeltme:** script, `npm` çalıştırmadan önce yalnızca **kendi süreci için** Node klasörünü ve System32'yi
  PATH'e ekliyor. Ayrıca deno'yu `tools\deno`'ya indiren bir adım eklendi (zaten varsa atlanır).
- **2. çalıştırma:** başarılı. 317 npm paketi kuruldu, `npx tsc` derlendi, smoke check
  `3.11.9 cuda True 2`, yt-dlp `2026.10.21`. Loglar: `logs\ytdlp_fix\setup_venv_run1.log`, `setup_venv_run2.log`.

## 3. Araştırma (2026-10-22) ve denenecek yöntem sırası

- **yt-dlp 2026.10.21** (21 Eki) çıktı. YouTube değişiklikleri: `tv_samsung` client eklendi ve varsayılan
  yapıldı, initial data extraction düzeltmesi. **2026.08.19:** `android_vr` varsayılan client'lardan çıkarıldı.
- **Eski kodumuz** `player_client=android_vr,web,tv`'yi zorluyordu. 19 Ekim'deki 403 hatası
  (`android_vr client https formats require a GVS PO Token … HTTP Error 403`) bunun sonucuydu.
- **EJS wiki:** JS challenge'ları için **deno önerilir** (≥ 2.3.0, varsayılan açık). node ≥ 22 de desteklenir
  (`--js-runtimes node:<yol>`). Çözücü script'leri `pip install -U "yt-dlp[default]"` (yt-dlp-ejs) ile gelir.
- **PO-Token-Guide:** öneri "PO token sağlayıcı eklentisi (bgutil) + `mweb` client". Elle token çıkarmak artık önerilmiyor.
- **Son issue'lar:** #17647 (403, kapandı: güncelleme ya da nightly ile düzeldi), #17644 (bgutil + mweb ile
  çalıştı), #17837 (açık: uzun oturumlarda hesap çerezleri dönüyor ve 403 başlıyor).
- **Nightly kanalı:** wiki'ye göre "normal kullanıcılar için önerilen kanal".

Denenecek sıra (görevdeki gibi): **a)** güncel yt-dlp + EJS + deno, varsayılan client'lar →
**b)** + bgutil sunucusu → **c)** + taze çerez → **d)** player_client denemeleri (mweb vb.). İlk tam çalışan
kombinasyonda durulacak.

## 4. Denemeler (`scripts/ytdlp_probe.py`, sonuçlar `logs/ytdlp_fix/probe_results.jsonl`)

| Deneme | Videolar | Sonuç | Not |
|---|---|---|---|
| a — yt-dlp 2026.10.21 + EJS + deno, varsayılan client'lar, çerez yok, PO sunucusu kapalı | jNQXAC9IVRU, BaW_jenozKc, dQw4w9WgXcQ | 1/3 | 2 video tüm client'larda "This video is unavailable" verdi. **oEmbed de 404 döndü**, yani bu bağlantıdan erişilemiyorlar; yöntemle ilgili değil. Rick Astley indirildi, 403 yok. |
| b — a + bgutil sunucusu (Node, 127.0.0.1:4416, v2.0.2) | aynı 3 video | 1/3 | Aynı iki video yine "unavailable". Varsayılan client'lar PO token istemediği için sunucu hiç kullanılmadı. |
| a (tekrar) — test seti oEmbed ile doğrulanmış videolarla değiştirildi | 9bZkp7q19f0, TcMBFSGVi1c, R6MlUcmOul8 | **3/3** | 16.3 + 4.9 + 36.0 MB, 8–12 sn. `[jsc:deno] Solving JS challenges using deno`. **İlk tam çalışan kombinasyon → durdum.** |
| c — taze çerez | — | denenmedi | Gerek kalmadı. Çerez durumu §6'da. |
| d — farklı player_client | — | denenmedi | Gerek kalmadı. Yedek olarak ayar hazır (§5). |

## 5. Çalışan yöntem ve kod bağlantısı

`downloader._base_args()` tüm yt-dlp çağrıları için ortak: indirme, kaynak keşfi (ytsearch), kanal listesi,
son yüklemeler kontrolü (hız şeridi) ve konuşma örneği indirme. Hepsi `.env`'den okunur:

| Ayar | Değer (doğrulanan) | Anlamı |
|---|---|---|
| `YTDLP_PYTHON` | boş → `.venv\Scripts\python.exe` | yt-dlp'nin çalıştığı Python |
| `YTDLP_JS_RUNTIME` | `deno` | `node` da olur (Node ≥ 22) |
| `YTDLP_JS_RUNTIME_PATH` | boş → `tools\deno\deno.exe` | tam yol |
| `YTDLP_PLAYER_CLIENT` | boş = yt-dlp varsayılanı | yedek seçenek: `mweb` (+ PO sunucusu) |
| `YTDLP_REMOTE_COMPONENTS` | boş | çözücü pip paketinden gelir. Gerekirse `ejs:github` |
| `YTDLP_USE_COOKIES` | `false` | hesap çerezleri gerekmedikçe gönderilmez |
| `POT_PROVIDER_URL` | boş = 127.0.0.1:4416 | bgutil sunucusu farklı adresteyse |
| `YTDLP_UPDATE_CHANNEL` | `stable` | `nightly` → `pip --pre` |

Sonuçta gönderilen komut: `.venv\Scripts\python.exe -m yt_dlp --js-runtimes deno:<repo>\tools\deno\deno.exe …`

## 6. Doğrulama (`scripts/ytdlp_verify.py`, üretim kod yolları, gerçek `.env`)

Sonuç dosyası `logs/ytdlp_fix/verify_20261022T151554Z.json` — **PASS**:

| Adım | Sonuç |
|---|---|
| ytsearch ("podcast full episode interview", 25) | 25 sonuç, 12'si 30–70 dk, 2.4 sn |
| Kanal son 3 video (`@BlenderOfficial`) | 3/3 listelendi, 1.0 sn |
| İndirme 1 — podcast (666OF8M-cJQ) | ✅ 59 dk 19 sn, 1080p VP9, 803 MB, 235 sn |
| İndirme 2 — aqz-KE-bpKQ | ✅ 10.6 dk, 1080p AV1, 155 MB, 12 sn |
| İndirme 3 — eRsGyueVLvQ | ✅ 14.8 dk, 872p AV1, 189 MB, 25 sn |
| İndirme 4 — YQHsXMglC9A | ✅ 6.1 dk, 1080p VP9, 109 MB, 24 sn |
| İndirme 5 — kJQP7kiw5Fk | ✅ 4.7 dk, 1080p VP9, 157 MB, 33 sn |

Her dosyada video ve ses akışı ffprobe ile kontrol edildi, sonra dosyalar silindi. İndirmeler arasında
15–45 sn rastgele bekleme vardı. **Toplam başarılı indirme: 10** (5 deneme + 5 doğrulama). Ayrıca
"unavailable" videolarda 4 başarısız deneme oldu; bunlarda medya indirilmedi.

Kanal kontrolündeki ilk denemem `@Blender` ile başarısız oldu ("This channel does not have a videos tab"):
o handle resmî kanal değil. Doğru adres oEmbed'den alındı: `@BlenderOfficial`.

**Format notu:** yt-dlp en iyi kaliteyi seçtiği için dosyalar AV1/VP9 video içeren MP4 oluyor. Bu FFmpeg
derlemesi ikisini de çözebiliyor (`libaom-av1`, `av1_cuvid` = RTX 3060 NVDEC, `vp9`). Render daha yavaş
olursa formatı H.264'e kısıtlamak bir seçenek: `-f "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/b"`.
Bu seçenek denenmedi.

## 7. PO token sunucusu (bgutil)

- Kurulum: `setup_venv.ps1` → `tools\bgutil-ytdlp-pot-provider` (git tag `2.0.2`) → `server\` → `npm ci` → `npx tsc`.
- Elle başlatma: `"C:\Program Files\nodejs\node.exe" tools\bgutil-ytdlp-pot-provider\server\build\main.js`
  → `Started POT server (v2.0.2) on address [::1]:4416, 127.0.0.1:4416`. Kontrol: `curl http://127.0.0.1:4416/ping`.
- Otomatik başlatma: `install_autostart.ps1` içinde ayrı görev **`Reels-PotProvider`** (çalıştırılmadı).
- **Şu anki yöntem bu sunucuyu kullanmıyor**, çünkü varsayılan client'lar token istemiyor. Sunucu yedek
  olarak hazır duruyor: YouTube varsayılan client'ları da kısıtlarsa `.env`'de `YTDLP_PLAYER_CLIENT=mweb`
  yap (PO-Token-Guide önerisi). Bu kombinasyon bu PC'de **doğrulanmadı**.

## 8. Çerezler (cookies.txt)

- **Durum:** 17 çerez, giriş yapılmış bir hesap (`__Secure-3PSID` vb.). Dosya ilk olarak ~**12 Ağustos**'ta
  dışa aktarılmış (yedekteki tarih). 19 Ekim tarihli değişiklik, yt-dlp'nin dönen çerezleri dosyaya geri
  yazmasından geliyor. Yani dosya yaklaşık **2 aylık**.
- **Şu an kullanılmıyor** (`YTDLP_USE_COOKIES=false`). Herkese açık videolar için gerekli değil ve yt-dlp wiki
  hesabın banlanma riskine karşı uyarıyor (gerekirse ayrı bir hesap kullan).
- **Gerekirse taze çerez** (yt-dlp wiki "Exporting YouTube cookies"):
  1. Tarayıcıda yeni bir **gizli/özel pencere** aç, YouTube'a giriş yap.
  2. Aynı sekmede `https://www.youtube.com/robots.txt` adresine git. Bu, açık olan **tek** gizli sekme olmalı.
  3. Bir çerez dışa aktarma eklentisiyle (yt-dlp FAQ'daki önerilenlerden biri) **youtube.com** çerezlerini
     Netscape formatında `cookies.txt` olarak kaydet.
  4. **Gizli pencereyi hemen kapat.** O oturum tarayıcıda bir daha açılmasın, yoksa çerezler döner.
  5. Dosyayı proje köküne `cookies.txt` olarak koy ve `.env`'de `YTDLP_USE_COOKIES=true` yap.
  - `--cookies-from-browser` ile **dışa aktarma**: bu yöntem gizli oturumu değil, normal tarayıcı çerezlerini alır.

## 9. Bakım notları

- **yt-dlp:** YouTube sık değiştiği için güncel kalmalı. `cycle_runner` her döngüde (3 günde bir) `.venv`'de
  `pip install -U "yt-dlp[default]" bgutil-ytdlp-pot-provider` çalıştırıyor. Kendine ayrıca bir zamanlayıcı
  kurmana gerek yok. 403 / "Sign in to confirm" yeniden başlarsa önce `.env`'de
  `YTDLP_UPDATE_CHANNEL=nightly` yap (wiki: "regular users" için önerilen kanal).
- **deno:** ayda bir `tools\deno\deno.exe upgrade`. Minimum sürüm 2.3.0.
- **bgutil:** güvenlik sürümleri çıkıyor (2.0.0 bir RCE düzeltmesiydi). Yeni tag çıktığında:
  `cd tools\bgutil-ytdlp-pot-provider` → `git fetch --tags` → `git checkout <yeni-tag>` → `cd server` →
  `npm ci` → `npx tsc` → `Reels-PotProvider` görevini yeniden başlat. Pip eklentisi döngü güncellemesiyle zaten yenileniyor.
- **Hızlı teşhis:** `.venv\Scripts\python.exe scripts\ytdlp_probe.py --label test --js deno <3 URL>`
  (sonuçlar `logs\ytdlp_fix\probe_results.jsonl`'a yazılır, son 40 satır dahil). Tam kontrol için
  `scripts\ytdlp_verify.py` (5 indirme yapar).
- **Belirti → yapılacak:**
  - `HTTP Error 403` → güncelle (stable, olmazsa nightly) → `YTDLP_PLAYER_CLIENT=mweb` + PO sunucusu.
  - `n challenge solving failed` → JS runtime bulunamıyor (`YTDLP_JS_RUNTIME_PATH`'i kontrol et).
  - `Sign in to confirm you're not a bot` → IP kısıtlaması: daha az istek, sonra taze çerez (§8).
  - `This video is unavailable` (tüm client'larda) → önce oEmbed ile videonun erişilebilir olup olmadığını kontrol et.
- Sistem bu hataları "YouTube engeli" olarak tanır (`downloader.classify_ytdlp_error`): indirmeleri durdurur
  ve `YOUTUBE_BLOCKED` alarmı + e-posta gönderir.
