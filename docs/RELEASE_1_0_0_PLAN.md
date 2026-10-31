# Релиз 1.0.0 на всех четырёх платформах (2026-10-30) — ВЫПОЛНЕНО

Опубликован (коммит 99c9ba8). Все пункты «Что осталось» сделаны; windows, linux-x86_64, xopek, beroun проверены клиентом из репозитория как версия 0.9.0: у всех статус `ready`, у windows в статусе есть `payload`. Xopek-оверлей пересобран с `sw_service_keys_local.py`; beroun пересобран из того же дерева (без файла ключей).

Продолжение `docs/UPDATE_EVERYWHERE_PLAN.md`. Сейчас опубликован релиз 1.0.0 только с `beroun` и `xopek`. Цель: один релиз 1.0.0 с `windows`, `linux-x86_64`, `beroun`, `xopek`, все артефакты из одного коммита, подписаны одним ключом.

## Уже сделано

* Ключ подписи скопирован в Notes: «Claude Credentials» → `vault:release_signing_key:caroline`. Локальный файл: `C:\Users\Konstantin Khait\.caroline\release-signing.key`.
* Коммит 659d258 (запушен): в подписанном манифесте у артефакта `windows` есть `payload` (имя и sha256 для `Caroline.zip`). Бэкенд отдаёт его в `/api/update/status`, `UpdateChecker.cs` передаёт установщику `--zip-file` и `--zip-sha256`, установщик берёт хеш из подписанного манифеста, а не из неподписанного `.sha256`. `tools/release.py build --artifact windows=ПУТЬ,payload=ПУТЬ_К_ZIP`; `verify` проверяет и payload. 49 тестов обновлений проходят.
* Windows собран: `make installer` завершился (`D:\REPO\caroline\dist_installer`: `Caroline.zip`, `CarolineInstaller.exe`).
* Linux: в WSL (`~/.intellicat/lin-build/src`) собран `dist-linux`, развёрнут рантайм, поставлены npm-пакеты. Остановка на последнем шаге: `appimagetool` отказал из-за прав 0744 у файлов `node_modules`.

## Что осталось

1. **AppImage.** В `linux-shell/build_appimage.sh` перед запуском `appimagetool` добавить `chmod -R a+rX "$APPDIR"`, закоммитить, перезапустить сборку в WSL (`Caroline-1.0.0-x86_64.AppImage`).
2. **Xopek-оверлей пересобрать.** Опубликованный `caroline-xopek-1.0.0.zip` собран из чистого клона, без `backend-py/app/sw_service_keys_local.py` (он в .gitignore). Оверлей меняет папку `app` целиком, значит на рабочем столе файл ключей пропал бы и `sw_api` не импортировался бы. Пересобрать из дерева с этим файлом (как `Caroline.zip` и AppImage, где он уже есть) и перезалить. Beroun не затронут: ключи ему даёт `sitecustomize` из `~/.intellicat/caroline-keys.py`.
3. **Переименовать для релиза:** `CarolineInstaller.exe` → `CarolineInstaller-1.0.0.exe`, `Caroline.zip` → `Caroline-1.0.0.zip` (в манифесте имена версионные; старые `Caroline.zip`, `.sha256`, `.version`, `CarolineInstaller.exe` не трогать — их читают установленные Windows-сборки).
4. **Собрать манифест:** `python tools/release.py build --version 1.0.0 --key <ключ> --out <папка> --artifact windows=CarolineInstaller-1.0.0.exe,payload=Caroline-1.0.0.zip --artifact linux-x86_64=Caroline-1.0.0-x86_64.AppImage --artifact xopek=... --artifact beroun=...` (у xopek и beroun `runtime=1`), затем `release.py verify`.
5. **Загрузить** через `uploader.exe` (токен из `silmarillion\Uploader\deploy_token.txt`, `-http -host downloader.multi-portal.org -port 41981`, `/var/www/html/apps/caroline/`): сначала все артефакты и `Caroline-1.0.0.zip` (~450 МБ), **последними** `release.json` и `release.json.sig`.
6. **Проверить снаружи** клиентом из репозитория для каждой платформы (притвориться версией 0.9.0): все четыре должны дойти до `ready`; у windows в статусе есть `payload`.
7. **Обновить** `UPDATE_EVERYWHERE_PLAN.md` (этапы 1, 3, 4 и «Осталось»), закоммитить и запушить.
8. **Убрать за собой:** `~/.intellicat/lin-build`, `~/.intellicat/rel-build` (уже удалён), временные файлы.

## Не входит сюда

* Живой прогон обновления на настоящих Windows, Linux и Xopek-образе (нужен релиз выше 1.0.0 и установленная сборка с новым клиентом).
* Этап 7 — Android (придёт вместе с Android-оболочкой).
* Ветка «Caroline занята → спросить» на Beroun вживую.
