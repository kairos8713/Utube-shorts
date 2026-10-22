# Sabah Raporu — utube-shorts → Otonom Instagram Reels (gece çalışması 2026-10-19)

Branch: `instagram-migration` (yerel, **push yapılmadı**). Git artık bu PC'de kurulu; gece çalışmasındaki
ilk commit'ler git yokken `python scripts/dev_git.py` (dulwich) ile atılmıştı, sonrakiler normal git ile. Her şey `DRY_RUN=true` ile çalışıyor: Instagram'a,
e-postaya, R2'ye hiçbir istek gitmedi.

## 1. Fazlar

| Faz | Durum | Not |
|---|---|---|
| 0 Hazırlık | ✅ | Yedek: `backups/pre_migration_20261019/` (DB, tokens, sessions, cookies, json). Baseline commit. |
| 1 Güvenlik | ✅ | `.env` oluşturuldu (Fernet + panel secret üretildi, SMTP `email_config.json`'dan taşındı). DB'deki düz metin şifre şifrelendi. Panel 127.0.0.1, hash'li şifre, rate limit, CSRF. |
| 2 Temizlik + IG formatı | ✅ | 30 fps + RIFE ×2 = 60 fps, 8 Mbps, AAC 48 kHz, −14 LUFS, faststart. Paneller/outro/SFX/ilerleme çubuğu yok. Render QC kapısı. Kredi hatası düzeltildi. |
| 3 Qwen 32B / Ollama + GPU kilidi | ⚠️ **BLOKE (kısmi)** | Ollama kurulu değil, `C:\models\Qwen3-32B-Q4_K_M.gguf` **diskte yok**. İstemci + Modelfile yazıldı, her şey mock ile test edildi. GPU kilidi çalışıyor. |
| 4 3 günlük döngü, şema, guardrail, beyin | ✅ | Gerçek DB migrate edildi (yedekli). |
| 5 Instagram yayıncısı + zamanlayıcı | ✅ (DRY_RUN) | Meta dokümantasyonu web'den doğrulandı (DECISIONS D-020). |
| 6 Yorum yanıtlayıcı | ✅ (DRY_RUN) | |
| 7 Kaynak keşfi / test / hız şeridi | ✅ (mock'lu test) | Gerçek YouTube indirmesi şu an **403** veriyor, aşağıya bak. |
| 8 Analitik | ✅ | |
| 9 Web paneli | ✅ | Tüm sayfalar testte hatasız açılıyor. |
| 10 Dayanıklılık | ✅ | Görev Zamanlayıcı script'i yazıldı, **çalıştırılmadı**. |
| 11 Testler + uçtan uca kuru çalıştırma | ✅ | 203 test geçti; E2E PASS (sentetik kaynak, mock Qwen) |
| 12 Bu rapor | ✅ | |

## 2. Test sonuçları ve kuru çalıştırma

- `python -m pytest -q tests` → **203 passed** (~33 sn). Kapsam: guardrail'ler, Qwen eylem
  doğrulayıcı (yasak/aşırı/kapsam dışı öneriler reddediliyor), prompt injection (talimatlı yorum →
  link/@ içeren yanıt engellendi, dış metin hep veri bloğunda), zamanlayıcı (8 dk boşluk, tam %25
  keşif, kaçan paylaşım dağıtımı, **25 Ekim 2026 DST**: pencere 15 saat), çift yayın koruması,
  otomatik durdurma, token yenileme, pHash tekrar kontrolü, render QC, e-posta tekilleştirme,
  migration'lar, tüm panel sayfaları, CSRF. Instagram ve Ollama mock'lu.
- Uçtan uca DRY_RUN (`python scripts/e2e_dry_run.py --synthetic`): **PASS** — 13/13 kontrol.
  Akış: sentetik kaynak (76 sn, SAPI konuşma) → Whisper (11 sn, 60 kelime zamanlı) → mock Qwen →
  render + RIFE (88 sn; RIFE 57 sn) → QC → kapak (19.8 sn) → kuyruk → simüle yayın (istekler sadece
  loglandı) → 4 simüle yorum: soru → yanıtlandı (dry), "my video… take it down" → RISKY + outbox
  e-postası, spam linki → gizlendi (dry), emoji → yanıtsız → rapor. Yayından sonra reel dosyası silindi.

| Kontrol | Sonuç |
|---|---|
| Çözünürlük | 1080×1920 ✅ |
| FPS | 60.0 ✅ (RIFE kullanıldı) |
| Video / ses codec | h264 / AAC 48 kHz ✅ |
| Bit hızı | 8.52 Mbps ✅ |
| faststart (moov önde) | ✅ |
| Ses yüksekliği | −13.8 LUFS, true peak −1.5 dBFS ✅ |
| Süre / boyut | 22.0 sn / 23.4 MB ✅ |
| İlk kare karartılmamış | merkez parlaklık 127 ✅ |
| Kutu/panel/buton yok | üst/alt bölge keskinliği 0.3 (sadece bulanık arka plan) ✅ |
| Altyazı güvenli alanda | y≈1290–1420 bandında ✅ (gözle de kontrol edildi) |
| Kredi görünür | "@E2E_Source" x=60, y=590 ✅ |
| Caption | "Hard to argue with this once you hear the numbers behind it." + 3 hashtag + `🎥 @E2E_Source` |

Kareler ve tam sonuç: `logs/e2e/20261018T235254Z/` (`frame_0.5s.png`, `frame_middle.png`,
`frame_end.png`, `reel.mp4`, `e2e_result.json`, `logs/outbox/`). Gerçek YouTube indirmesi denendi; **HTTP 403** (aşağıda) yüzünden sentetik video
(testsrc + Windows SAPI ile üretilmiş konuşma) kullanıldı — Whisper, altyazı, render, RIFE, QC gerçek
çalıştı; Qwen yerine mock kullanıldı.

## 3. Emre'nin yapması gerekenler (sırayla)

1. **YouTube indirmesi — ÇÖZÜLDÜ (2026-10-22), ayrıntı `docs/YTDLP_FIX.md`:** `.venv` + yt-dlp 2026.10.21 +
   yt-dlp-ejs + deno (`tools\deno`), yt-dlp'nin varsayılan client'ları, çerezsiz: 5 indirme (59 dk podcast dahil),
   ytsearch ve kanal listesi doğrulandı. Eski 403'ün sebebi eski yt-dlp + kodun `android_vr`'ı zorlamasıydı.
   Sistem PATH'i 2026-10-22'de düzeltildi (System32, nodejs, Python311). bgutil PO token sunucusu yedek
   olarak hazır (`Reels-PotProvider` görevi).
   **2026-10-23:** tek bir videodaki 403 artık döngüyü durdurmuyor (60–120 sn bekle, avc1 formatla 1 kez daha
   dene, olmazsa sadece o video atlanır); `YOUTUBE_BLOCKED` ancak 1 saatte ≥ 3 farklı videoda 403/429 olursa
   ya da "Sign in to confirm you're not a bot" görülürse çalar. Alarm metni türe göre doğru çözümü söyler.
2. **Sızmış anahtarlar:** Bu kopyada `youtube_analytics.py` yoktu, ama orijinal public repoda
   hardcoded YouTube API anahtarı varsa **Google Cloud Console → Credentials'tan iptal et**
   (git geçmişi yeniden yazılmadı). `client_secret.json` (Google OAuth) da public repoda olduysa
   onu da yenile. Eski panelin `root/toor` girişi kaldırıldı.
3. **`.env` değerleri** (`.env` oluşturuldu; `.env.example` referans):
   - Panel şifresi: `python scripts/init_secrets.py --set-panel-password` (şu an giriş kapalı).
   - `FERNET_KEY`'i bir şifre yöneticisine kopyala. Kaybolursa tüm token'lar okunamaz.
   - `IG_APP_ID`, `IG_APP_SECRET`, `IG_REDIRECT_URI`.
   - `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` (zorunlu, aşağıdaki not);
     `R2_PUBLIC_BASE_URL` sadece `R2_URL_MODE=public` ise gerekli.
   - E-posta: SMTP değerleri taşındı; gerçek gönderim için `EMAIL_ENABLED=true` ve
     `EMAIL_TEST_MODE=false` yap. Sonra `email_config.json`'ı sil.
   - Hazır olunca `DRY_RUN=false`.
4. **Meta uygulaması:** developers.facebook.com → uygulama oluştur → "Instagram" ürünü →
   "API setup with Instagram login". İzinler: `instagram_business_basic`,
   `instagram_business_content_publish`, `instagram_business_manage_comments`,
   `instagram_business_manage_insights`. Hesapları Instagram Tester olarak ekle (uygulama
   Development modundayken). OAuth redirect URI: `https://localhost:5050/oauth/instagram/callback`
   (Meta HTTPS istiyor; panel HTTP olduğu için tarayıcı sayfayı açamazsa adres çubuğundaki URL'yi
   paneldeki "OAuth geri dönüşü panele ulaşmadıysa" kutusuna yapıştır).
5. **Video barındırma (R2) zorunlu:** Meta dokümantasyonuna göre resumable upload yalnızca
   *Facebook Login for Business* uygulamalarında var. Instagram Login ile video bir **public URL**
   (`video_url`) üzerinden alınmalı → `IG_UPLOAD_METHOD=r2_url` (varsayılan). Varsayılan
   `R2_URL_MODE=presigned`: bucket **private** kalır, Instagram'a 1 saat geçerli imzalı GET URL'si verilir
   (`R2_PUBLIC_BASE_URL` gerekmez). ⚠️ Instagram'ın imzalı URL'yi kabul ettiği **doğrulanmadı** — ilk gerçek
   yayında kontrol et; container ERROR verirse `R2_URL_MODE=public` + bucket'a public erişim (r2.dev veya
   kendi alan adın) + `R2_PUBLIC_BASE_URL`. Dosyalar yayından hemen sonra silinir.
6. **Hesaplar:** Her hesabı Professional (Creator/Business) yap → panelde *Hesaplar → Yeni hesap*
   → *Hesap Bağla* → hesabın kurulum/ısınma tamamlanma tarihini gir. İlk paylaşım bu tarihten
   **2 gün sonra**. Tarih girilmeyen hesaba hiç yükleme yapılmaz.
7. **Ollama + model:**
   - Ollama'yı kur (ollama.com), sistem değişkenleri: `OLLAMA_FLASH_ATTENTION=1`,
     `OLLAMA_KV_CACHE_TYPE=q8_0`.
   - Model (tam test 1e, D-030): **`qwen3.8:27b`**. `.env`: `OLLAMA_MODEL=qwen3.8:27b`, `OLLAMA_NUM_GPU=99`,
     `OLLAMA_NUM_CTX=12288` (bu makinede ayarlı). GGUF / Modelfile **gerekmiyor**.
   - qwen3:32b de çalışır (ctx 6144'te %100 GPU) ama daha yavaş ve testte bir kez bilgi uydurdu.
   - Test: `ollama ps` → PROCESSOR sütunu `100% GPU` olmalı. cycle_runner açılışta bunu kendisi kontrol eder;
     CPU'ya taşma varsa `LLM_CPU_OFFLOAD` uyarı alarmı verir. Watchdog Ollama'yı yeniden başlatırsa log +
     `OLLAMA_RESTARTED` uyarısı yazar.
8. **Otomatik başlatma:** (önce 1. maddedeki `setup_venv.ps1`) →
   `powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1`. Tüm Python servisleri
   `.venv\Scripts\pythonw.exe` ile çalışır; 6 görev: PO token sunucusu (node, varsa), panel, watchdog,
   publisher, comments, cycle runner. Servisleri durdurmak için:
   `powershell -ExecutionPolicy Bypass -File scripts\stop_services.ps1` (görevleri durdurur ama silmez, alt
   süreçleri de kapatır; `-DryRun` sadece listeler). Aynı servis iki kez başlatılırsa ikinci kopya kendini
   kapatır (tek-örnek kilidi). Windows Update → Gelişmiş →
   **Etkin saatler**'i yayın penceresini (15:00–05:00 Warsaw) kapsayacak şekilde ayarla; prizdeki
   güç planında uykuyu kapat.
9. **Long-form pipeline (utube-lfv / llama-server):** başka bir PC'de çalışıyor; bu makinenin GPU'larını
   paylaşmadığı için ortak GPU kilidine gerek yok. (`GPU_LOCK_PATH` bu PC'deki servisler arasında kullanılmaya
   devam ediyor: Whisper, Qwen, render ve yorum servisi aynı anda GPU'ya girmez.)
10. **Arka plan müziği lisansı:** `bg_music/*_riskli` parçaları YouTube Audio Library'den görünüyor
    (The Grey Room, Patrick Patrikios…). Bu lisans YouTube içindir; Instagram sesi susturabilir veya
    engelleyebilir. Bu yüzden müzik artık **varsayılan olarak kapalı** (`BG_MUSIC_ENABLED=false`, render'a
    müzik girdisi hiç eklenmez). Instagram'da kullanım hakkı olan müzik koyarsan `true` yap.
11. **Kaynak:** Eski havuzdaki tek kaynak `@MrBeast` TRIAL olarak taşındı. Çok kesilen, telif
    koruması güçlü bir kanal; sistem test sonucuna göre karar verecek ama baştan çıkarmak isteyebilirsin
    (panel → Kaynaklar → Kara liste).
    **Kaynak kuralları (2026-10-23):** keşif artık TED/TEDx, haber/TV kanalları, film stüdyoları, müzik
    şirketleri (düzenlenebilir liste: `.env` → `SOURCE_EXCLUDE_CHANNEL_IDS`, `SOURCE_EXCLUDE_NAME_PATTERNS`),
    Music kategorisindeki ve tepki/derleme formatındaki kanalları eliyor. Mevcut kaynakları bu kurallara göre
    gözden geçirmek için (hiçbir şeyi değiştirmez, sadece öneri yazdırır):
    `.venv\Scripts\python.exe scripts\review_sources.py` (`--online` ile kategori/açıklama da kontrol eder);
    önerileri panelden uygula.
12. **Deneme önerisi:** İlk 1–2 hafta 4–5 hesapla başla; günlük 1 paylaşım (14 sağlıklı günden sonra
    sistem 2'ye çıkarabilir). Panelde Karar günlüğü ve Raporlar'ı her gün kontrol et.
    **Günlük sınır (2026-10-23, D-035):** "gün" = Warsaw yayın-penceresi günü (15:00–05:00, pencerenin
    başladığı tarih); aynı hesabın iki paylaşımı arası en az 14 saat (cap 2'de 6 saat); genel 8 dk aralık aynı.
13. **Canlıya geçmeden önce (tam test, `docs/FULL_TEST_REPORT.md` §11–12):** önce gerçek hesaplarla
    `DRY_RUN=true` bir döngü çalıştır, panelde "Bugünün planı"nı ve reelleri kontrol et (klipler artık cümle
    sınırında başlayıp bitiyor; yorum yanıtları küratör sesiyle). Sonra `DRY_RUN=false` ve ilk yayını canlı izle.
    `install_autostart.ps1`'i en son sen çalıştır.

## 4. Doğrulanamayan / doğrulanması gereken noktalar

- Ollama'nın bu özel TEMPLATE ile `think:false`'ı uygulaması (yedek: `/no_think` + `<think>` temizleme).
- Yorum gizleme parametresi `hide=true` ve yorum alanı `username` (dokümanda görülmedi, bilgiden).
- Takipçi sayısı alanı `GET /me?fields=followers_count`; `online_followers` metriği güncel listede yok
  (opsiyonel, yoksa atlanıyor).
- Yayın limiti: dokümanlar çelişkili (50 vs 100 / 24 saat); kod `content_publishing_limit` cevabını kullanıyor.
- Hata kodu sınıflandırması (190, 368, 2207042, 2207051…) genel Graph API bilgisine dayanıyor.
- API sürümü `v25.0` sabitlendi (IG dokümanları bunu gösteriyor); en yeni Graph sürümü `v26.0`.
  Bir gerçek test paylaşımından sonra `IG_API_VERSION=v26.0` denenebilir.
- Polling: dokümanlar "dakikada bir, en fazla 5 dk" diyor; Reels işlenmesi uzun sürebildiği için
  zaman aşımı 10 dk.
- Copyright: container'daki `copyright_check_status.matches_found=true` olursa yayın iptal + kaynak kara liste.
- R2 imzalı (presigned) URL'nin Instagram'ın `video_url` alıcısı tarafından kabul edilmesi (ilk gerçek yayın).
- bgutil PO token sunucusunun bu PC'de derlenip çalışması (script yazıldı, çalıştırılmadı).

## 5. Önemli kararlar (ayrıntı: `docs/DECISIONS.md`)

- D-003 Kopya repo zaten yarı IG'ye geçmişti; görevin *hedefleri* mevcut koda uygulandı.
- D-004 Panel şifresi üretilmedi; hash girilene kadar giriş kapalı (fail-closed).
- D-005 Altyazı ve kredi RIFE'tan **sonra** basılıyor (interpolasyon yazıyı bulandırmasın).
- D-006 RIFE başarısız olursa kare çoğaltma ile 60 fps, QC raporunda işaretli.
- D-008 `legacy_runner.py` → `downloader.py`, `transcriber.py`, `media_utils.py`; eskisi arşivde.
- D-011 Ollama/GGUF yok → mock; VRAM notu.
- D-012 Tek global GPU kilidi; Whisper/Qwen aşama boyu, render klip başına.
- D-013 Caption'ı kod birleştiriyor; `🎥 @kaynak` satırını model değil kod ekliyor.
- D-015 Günlük limitler kayan 24 saat.
- D-016 WARMUP→ACTIVE sadece ısınma kapısıyla; duraklatılmış hesabı sadece panel açar.
- D-017 pHash: kaynak klipten 4 kare, hepsi eşleşirse kopya.
- D-020 Instagram API doğrulama tablosu (r2_url varsayılan, v25.0).
- D-021 Yedeklerde Fernet anahtarı yok.
- D-022 5 süreç; hız şeridi cycle runner'da (yayını bloklamasın).
- D-024 YouTube 403/PO token = sistemik engel, indirmeler durur.

## 6. Dosyalar ve commit'ler

**Yeni (aktif):** `config.py`, `log_setup.py`, `crypto_utils.py`, `db_migrate.py`, `timeutil.py`,
`system_state.py`, `alerts.py`, `guardrails.py`, `brain/` (`actions.py`, `core.py`), `prompt_safety.py`,
`llm_client.py`, `llm_mock.py`, `ollama/Modelfile`, `gpu_lock.py`, `downloader.py`, `transcriber.py`,
`media_utils.py`, `production.py`, `production_store.py`, `dedup.py`, `cover_frame.py`, `credit.py`,
`render_qc.py`, `cycle_runner.py`, `scheduler.py`, `publish_runner.py`, `instagram/` (`client.py`,
`errors.py`, `oauth.py`, `hosting.py`, `publisher.py`, `token_refresh.py`), `comment_responder.py`,
`sources_manager.py`, `analytics.py`, `reports.py`, `maintenance.py`, `watchdog.py`, `power.py`,
`jobs.py`, `panel.py`, `synthetic_media.py`, `web/` (yeniden yazıldı: `auth.py`, `views.py`,
şablonlar), `scripts/` (`init_secrets.py`, `install_autostart.ps1`, `e2e_dry_run.py`, `dev_git.py`),
`requirements.txt`, `.env.example`, `.gitignore`, `tests/` (19 test dosyası + yardımcılar).

**Yeniden yazılan:** `ffmpeg_renderer.py`, `rife_interpolator.py`, `qwen_specialized.py`,
`email_notifier.py`, `models.py` (genişletildi).

**`legacy_youtube/`'a taşınan:** `publisher.py`, `weekly_cycle.py`, `sector_processor.py`,
`fulfillment_engine.py`, `legacy_runner.py`, `update_stats.py`, `verify_login.py`, `get_session_file.py`,
`setup_tokens.py`, eski panel (`web_routes.py`, `web_app.py`, `web_templates/`), `debug_*`/`fix_*`/
`migrate_*`/`check_*` tek seferlik script'ler, `deneme.py`, `cleanup*.py`, `add_channel.py`, `init_db.py`,
`test_analytics.py`, `test_pipeline.py`.

**`legacy_misc/`'e taşınan:** `daily_production.py`, `publish_on_connect.py` (resmi olmayan instagrapi +
ADB yaklaşımı — hesap riski, geri getirilmemeli), `ig_dashboard.py`, `ig_templates/`, `ig_static/`,
`migrate_ig_columns.py`, `esrgan_upscaler.py`, `test_render.py`, `multimodal_context.py`.

**Silinen:** yok (yinelenen `bg_music/tech__ai_riskli`, `food__cooking_riskli` klasörleri birleştirildi,
`automative_riskli` → `automotive_riskli`). Görevde geçen `ffmpeg_renderer_p2.py`, `youtube_analytics.py`,
`fetch_channel_stats.py`, `platform_config.py` bu kopyada yoktu.

**DB değişiklikleri** (her biri öncesinde `backups/migrations/` altında yedek): 001 şifre şifreleme,
002 Instagram şeması, 003 `sectors.music_slug`, 004 eski havuz → `sources` (TRIAL).

**Commit'ler** (`python scripts/dev_git.py log`):

| Commit | İçerik |
|---|---|
| 6c7b7c09f5 | Baseline: gece öncesi durum |
| 1110d847df | Faz 1 güvenlik |
| ec0bb574a4 | Faz 2 IG formatı, QC, temizlik |
| b7d613d069 | Faz 3 Ollama istemcisi, GPU kilidi, batch üretim |
| 57648c8911 | Faz 4 döngü, şema, guardrail, beyin |
| e575959f9f | Faz 5 Instagram yayıncısı + zamanlayıcı |
| fbdd34baf8 | Faz 6 yorum yanıtlayıcı |
| 7bfad71ebc | Faz 7 kaynak keşfi / bandit / hız şeridi |
| 4e12290052 | Faz 8 analitik |
| 2d55481ba7 | Faz 9 web paneli |
| 953c934134 | Faz 10 dayanıklılık |
| 84a2f3df87 | Faz 11 testler + E2E |
| (son) | Faz 12 bu rapor |

## 7. Servisleri elle denemek (DRY_RUN açıkken güvenli)

```
python db_migrate.py                     # şema güncel mi
python panel.py                          # http://127.0.0.1:5050
python scripts/e2e_dry_run.py --synthetic
python -m pytest -q tests
```
`cycle_runner.py` / `publish_runner.py` / `comment_responder.py` / `watchdog.py` sürekli çalışan
servislerdir. Ollama/model hazır değilse cycle_runner üretimi o döngü atlar ve panelde
`LLM_UNAVAILABLE` alarmı çıkar (mock'a asla düşmez; mock sadece testler ve E2E içindir).

## Düzeltmeler

İnceleme sonrası düzeltmeler (tek commit, `instagram-migration`; `DRY_RUN=true`, push yok).
Testler: **240 geçti** (yeni `tests/test_fixes.py`: 37 test).

1. **Çift yayın (kritik):** `media_publish` artık her container için **en fazla bir kez** çağrılıyor.
   Sonuç belirsizse (ağ/5xx/sınıflanmamış hata, ya da "container PUBLISHED ama medya yok") post `UNKNOWN`
   kalıyor, kuyruk öğesi `HELD`, hata sayacı artmıyor, otomatik yeniden deneme yok. Yayın sırasında öğe
   `PUBLISHING` durumunda; servis çökerse yeniden açılışta tekrar yayınlanmaz. Yeni
   `reconcile_uncertain()` (publish_runner açılışında + 10 dk'da bir): 10 dk'dan eski `CONTAINER/UNKNOWN`
   postlar için container durumu + son 25 medyada caption eşleşmesi → bulunduysa `PUBLISHED`; 2 saat
   sonra container yayınlanmamışsa post `FAILED`, öğe `PENDING` (normal yeniden deneme); 2 saat sonra
   hâlâ belirsizse `PUBLISH_UNCERTAIN` CRITICAL alarm + e-posta, öğe `HELD`.
2. **Token sızıntısı:** `redact.py` — `access_token`, `client_secret`, uzun `code=` değerleri,
   `Authorization` başlıkları, `IGAA…` token'ları, çözülmüş token'lar ve `.env` sırları, R2 imza
   parametreleri maskeleniyor. IGError metinleri, HTTP transport istisnaları, OAuth akışı, token
   yenileme, **tüm log satırları (traceback dahil)**, alarm ve e-posta metinleri buradan geçiyor.
3. **Yorumla kara liste:** Tek yorum artık kaynağı kara listeye almıyor (sadece alarm + "Dikkat
   gerektirenler"). Otomatik kara liste yalnızca: son 7 günde aynı kaynağın kliplerinde **3 farklı
   kullanıcıdan** açık hak iddiası (copyright, dmca, takedown, without permission, reupload, telif) veya
   yorumu yazanın kullanıcı adı kaynağın handle'ıyla eşleşiyorsa. "stole", "my video" gibi ifadeler RISKY
   kalır ama sayıma girmez.
4. **Yavaş işleme:** `NOT_READY` (poll zaman aşımı) hata sayacını artırmıyor, otomatik duraklatmaya
   sayılmıyor; sadece `retry_count` artıyor ve 30 dk sonra tekrar deneniyor. (Ek olarak bulunan hata:
   `poll_timeout=0` sessizce 600 sn'ye dönüşüyordu — düzeltildi.)
5. **R2 imzalı URL:** `R2_URL_MODE=presigned` (varsayılan, 1 saatlik imzalı GET, bucket private,
   `R2_PUBLIC_BASE_URL` gerekmez) veya `public`. Instagram'ın imzalı URL'yi kabul ettiği ilk gerçek
   yayında doğrulanmalı (§3 madde 5).
6. **Müzik:** `BG_MUSIC_ENABLED=false` varsayılan; kapalıyken render'a müzik girdisi hiç eklenmiyor.
7. **FFmpeg sürümü:** cycle_runner açılışta FFmpeg ≥ 7 kontrol ediyor (sürüm numarası olmayan git
   build'lerde libavfilter ≥ 10). Düşükse `FFMPEG_TOO_OLD` CRITICAL alarm + e-posta ve servis başlamıyor.
   Bu PC: libavfilter 12.3 → geçti.
8. **Ayrı Python ortamı:** `scripts/setup_venv.ps1` (yeni) + `install_autostart.ps1` artık
   `.venv\Scripts\pythonw.exe` kullanıyor ve PO token sunucusunu (`Reels-PotProvider`) da kaydediyor.
   İkisi de **çalıştırılmadı** (sadece PowerShell parser ile sözdizimi kontrol edildi). `requirements.txt`'e
   eksik olan `torchvision` (RIFE `model/loss.py` istiyor) ve `bgutil-ytdlp-pot-provider` eklendi;
   yt-dlp sabit sürümden `>=`'e çevrildi. İndirici `YTDLP_PLAYER_CLIENTS` (o gün varsayılan `mweb,tv,web_safari`; 2026-10-22'de `YTDLP_PLAYER_CLIENT` = yt-dlp varsayılanı olarak değişti, bkz. YTDLP_FIX.md)
   ve isteğe bağlı `POT_PROVIDER_URL` kullanıyor.
9. **Ollama modeli:** `OLLAMA_MODEL` varsayılan `qwen3:32b`, `OLLAMA_NUM_CTX` varsayılan 8192, GGUF/Modelfile
   opsiyonel. Model sağlık kontrolü artık tam etiketle eşleşiyor (önceden herhangi bir `qwen3:*` yeterliydi).
   Açılışta model yüklenip `GET /api/ps` (`ollama ps` ile aynı veri; API yoksa `ollama ps` çıktısı
   ayrıştırılıyor) ile GPU payı kontrol ediliyor; %99'un altındaysa `LLM_CPU_OFFLOAD` uyarı alarmı.

**Yerel `.env` güncellendi** (commit'e girmez): dün geceki `.env` eski varsayılanlardan üretildiği için
`IG_API_VERSION=v24.0`, `IG_UPLOAD_METHOD=resumable`, `OLLAMA_MODEL=qwen3-32b-local` değerlerini sabitliyordu ve
yeni varsayılanları geçersiz kılıyordu. Bunlar `v25.0`, `r2_url`, `qwen3:32b` yapıldı; `OLLAMA_NUM_CTX=8192`,
`R2_URL_MODE=presigned`, `BG_MUSIC_ENABLED=false`, `YTDLP_PLAYER_CLIENTS` eklendi. Gizli değerlere dokunulmadı;
`DRY_RUN=true` kaldı. `.env.example` yeniden üretildi.
