# Sabah Raporu — utube-shorts → Otonom Instagram Reels (gece çalışması 2026-10-19)

Branch: `instagram-migration` (yerel, **push yapılmadı**). Bu PC'de git kurulu değil; commit'ler
`python scripts/dev_git.py` (dulwich) ile atıldı. Her şey `DRY_RUN=true` ile çalışıyor: Instagram'a,
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
| 11 Testler + uçtan uca kuru çalıştırma | E2E_STATUS | |
| 12 Bu rapor | ✅ | |

## 2. Test sonuçları ve kuru çalıştırma

- `python -m pytest -q tests` → **TEST_COUNT**
- Uçtan uca DRY_RUN (`python scripts/e2e_dry_run.py --synthetic`): E2E_SUMMARY

E2E_TABLE

Kareler ve tam sonuç: `E2E_DIR` (`frame_0.5s.png`, `frame_middle.png`, `frame_end.png`, `reel.mp4`,
`e2e_result.json`). Gerçek YouTube indirmesi denendi; **HTTP 403** (aşağıda) yüzünden sentetik video
(testsrc + Windows SAPI ile üretilmiş konuşma) kullanıldı — Whisper, altyazı, render, RIFE, QC gerçek
çalıştı; Qwen yerine mock kullanıldı.

## 3. Emre'nin yapması gerekenler (sırayla)

1. **YouTube indirmesi bozuk (acil):** yt-dlp 2026.08.19 ile her indirme
   `HTTP Error 403` veriyor (android_vr istemcisi "GVS PO Token" istiyor). Çözüm sırası:
   `python -m pip install -U yt-dlp yt-dlp-ejs` → yine olmazsa PO Token eklentisi
   (yt-dlp wiki "PO-Token-Guide", ör. `bgutil-ytdlp-pot-provider`) ve taze `cookies.txt`.
   Sistem bu hatayı artık "YouTube engeli" sayıp indirmeleri durdurur ve alarm verir.
2. **Sızmış anahtarlar:** Bu kopyada `youtube_analytics.py` yoktu, ama orijinal public repoda
   hardcoded YouTube API anahtarı varsa **Google Cloud Console → Credentials'tan iptal et**
   (git geçmişi yeniden yazılmadı). `client_secret.json` (Google OAuth) da public repoda olduysa
   onu da yenile. Eski panelin `root/toor` girişi kaldırıldı.
3. **`.env` değerleri** (`.env` oluşturuldu; `.env.example` referans):
   - Panel şifresi: `python scripts/init_secrets.py --set-panel-password` (şu an giriş kapalı).
   - `FERNET_KEY`'i bir şifre yöneticisine kopyala. Kaybolursa tüm token'lar okunamaz.
   - `IG_APP_ID`, `IG_APP_SECRET`, `IG_REDIRECT_URI`.
   - `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE_URL`
     (zorunlu, aşağıdaki not).
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
   (`video_url`) üzerinden alınmalı → `IG_UPLOAD_METHOD=r2_url` (varsayılan). Cloudflare R2
   bucket'ı + public erişim (r2.dev veya kendi alan adın) aç; dosyalar yayından hemen sonra silinir.
6. **Hesaplar:** Her hesabı Professional (Creator/Business) yap → panelde *Hesaplar → Yeni hesap*
   → *Hesap Bağla* → hesabın kurulum/ısınma tamamlanma tarihini gir. İlk paylaşım bu tarihten
   **2 gün sonra**. Tarih girilmeyen hesaba hiç yükleme yapılmaz.
7. **Ollama + model:**
   - Ollama'yı kur (ollama.com), sistem değişkenleri: `OLLAMA_FLASH_ATTENTION=1`,
     `OLLAMA_KV_CACHE_TYPE=q8_0`.
   - GGUF'u `C:\models\Qwen3-32B-Q4_K_M.gguf` yoluna koy (gece **bulunamadı**).
   - `ollama create qwen3-32b-local -f ollama/Modelfile`
   - Test: `ollama run qwen3-32b-local "merhaba"`. 2×12 GB'a tam sığmazsa `.env`'de
     `OLLAMA_NUM_CTX=8192`.
8. **Otomatik başlatma:** `powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1`
   (5 görev: panel, watchdog, publisher, comments, cycle runner). Windows Update → Gelişmiş →
   **Etkin saatler**'i yayın penceresini (15:00–05:00 Warsaw) kapsayacak şekilde ayarla; prizdeki
   güç planında uykuyu kapat.
9. **Long-form pipeline (utube-lfv / llama-server):** aynı GPU kilidini kullanmalı:
   `filelock.FileLock(<GPU_LOCK_PATH>)` (varsayılan `C:\Users\Emre\gpu_shared.lock`), GPU işinden
   önce al, bitince bırak. Aksi halde iki pipeline aynı anda VRAM'e girer.
10. **Arka plan müziği lisansı:** `bg_music/*_riskli` parçaları YouTube Audio Library'den görünüyor
    (The Grey Room, Patrick Patrikios…). Bu lisans YouTube içindir; Instagram sesi susturabilir veya
    engelleyebilir. Instagram'da kullanım hakkı olan müzikle değiştir ya da klasörleri boşalt.
11. **Kaynak:** Eski havuzdaki tek kaynak `@MrBeast` TRIAL olarak taşındı. Çok kesilen, telif
    koruması güçlü bir kanal; sistem test sonucuna göre karar verecek ama baştan çıkarmak isteyebilirsin
    (panel → Kaynaklar → Kara liste).
12. **Deneme önerisi:** İlk 1–2 hafta 4–5 hesapla başla; günlük 1 paylaşım (14 sağlıklı günden sonra
    sistem 2'ye çıkarabilir). Panelde Karar günlüğü ve Raporlar'ı her gün kontrol et.

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
`requirements.txt`, `.env.example`, `.gitignore`, `tests/` (TEST_FILES dosya).

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

**Commit'ler:**

COMMITS
