# FULL TEST PROGRESS — yayın hariç tam sistem testi

> Oturum yeniden başlarsa önce bunu oku. Görev: `claude_code_full_test_prompt.md`.
> Python: `.venv\Scripts\python.exe`. Gerçek DB'ye dokunma; test klasörü `test_runs/full_<ts>/`.
> DRY_RUN=true. Push yok. Commit öncesi gizli-desen taraması.

| Adım | Durum | Not |
|---|---|---|
| 1a PUBLISHING düzeltmesi | GEÇTİ | commit 9482a44 |
| 1b Ollama | GEÇTİ | qwen3:32b 100% GPU ctx 6144; + fix 0f21d1c (load options, NUM_GPU) |
| 1c .venv + YouTube probe | GEÇTİ | 1 indirme (TcMBFSGVi1c) |
| 1e Model karşılaştırması | GEÇTİ | qwen3.8:27b, NUM_GPU 99, CTX 12288 (D-030) |
| 1d pytest | GEÇTİ | 257 passed |
| 2 Test ortamı | GEÇTİ | test_runs/full_20261022T164925Z |
| A | GEÇTİ | 4 niş × 3 aday, 4 TRIAL; bug fix 94a862b (handle); budget 5/8 (harness bug: A 4 örnek aldı) |
| C | KISMİ | 6 reel / 0 hata (Psychology: TEDx 403 PO_TOKEN; Tech/AI: bütçe bitti). Cümle sınırı: 6/6 cümle ortasında bitiyor (tasarım, rapor) |
| D | GEÇTİ | 6/6 yerleşti; ama takvim günü başına 1 ≠ guardrail'in kayan 24 saati (tasarım sorunu, rapor) |
| E plan | GEÇTİ (fix sonrası) | önce 4/6 (DEFER_CAP ertesi güne atıyordu) → fix d458134 → 6/6 |
| E inject | GEÇTİ | 6 senaryo; harness bug (ortak dummy dosya) düzeltildi; 'AUTH: AUTH:' kozmetik |
| B real | GEÇTİ | ACTIVE kaynak yok → 0 video (beklenen); 4 TRIAL kaynağın son 3 yüklemesi alındı |
| B priority | GEÇTİ | Money Guy en yeni video zaten kullanılmış → atlandı (doğru); Jay Shetty → öncelikli klip ilk gelecek slota |
| F | GEÇTİ | 30/30 etiket kabul edilebilir; yanıt içeriği zayıf (rapor) |
| G | GEÇTİ | yasak öneri 0 |
| H | GEÇTİ | panel 12 sayfa 200, 5 ekran görüntüsü (port 5071); fix efd8729 (YOUTUBE_BLOCKED metni) |
| I | GEÇTİ (fix sonrası) | 48,2 dk; açılışta çift cycle_runner bulundu → fix 840fb38 (tek-örnek kilidi) |
| J | GEÇTİ | gerçek DB hash aynı; süreç kalmadı; Ollama tray geri; boş WAL/shm silindi |
| Rapor | BİTTİ | docs/FULL_TEST_REPORT.md |

## Notlar
- 2026-10-22: Registry PATH düzeltilmiş (System32, nodejs, Python311). Ollama 0.40.6; qwen3:32b + qwen3.8:27b kurulu.
- Ortam çağrıları (bash) eski PATH'i görüyor; araçlar tam yolla çağrılıyor.

## Kararlar sonrası (2026-10-23, claude_code_decisions_prompt.md)

| Madde | Durum | Commit |
|---|---|---|
| 1 Klip sınırları cümleye hizalı | BİTTİ | b0b8d72 |
| 2 Pencere günü + aynı hesap aralığı | BİTTİ | df22371 |
| 3 Yorum yanıtları (küratör sesi) + F yeniden | BİTTİ | 8b27486 |
| 4 Tek 403 döngüyü durdurmuyor | BİTTİ | ca165e1 |
| 5 Kaynak kuralları + sıralama + A yeniden | BİTTİ | 9e1dea9 |
| 6 Küçük düzeltmeler + stop_services.ps1 | BİTTİ | 0f15a5f |
| 7 Rapor / MORNING_REPORT | BİTTİ | (docs commit) |

Testler: 299 passed. Push yok.
