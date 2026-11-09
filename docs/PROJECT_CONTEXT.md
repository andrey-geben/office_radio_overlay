# Контекст проекта: Office Radio Overlay (EVRAZ edition)

## О проекте
Система трансляции интернет-радио в офисе с автоматической вставкой служебных объявлений по меткам DTMF, поддержкой SIP/ЛСО и резервных источников. Разрабатывается для ЕВРАЗ.

- **GitHub:** https://github.com/andrey-geben/office_radio_overlay
- **Лицензия:** MIT
- **Статус:** v1.0.0 — работает, не запакован в .exe
- **Автор:** Andrey Geben

## Стек
- Python 3.12, Windows 11 x64
- Flask (веб-панель, API), Jinja2
- sounddevice (аппаратный вход/выход)
- pydub + FFmpeg 9.0.2 (декодирование MP3-потоков)
- numpy, scipy (FFT, фильтрация)
- pjsua2 (PJSUA2, SIP-клиент для ЛСО)
- psutil (мониторинг CPU/сети)
- PyInstaller (упаковка)

## Текущий статус — всё РАБОТАЕТ

| Компонент | Файл | Статус |
|---|---|---|
| Веб-панель (ретро, EVRAZ-стиль) | templates/index.html, static/ | ✅ |
| Авторизация admin/operator + смена пароля | auth.py, users_store.py | ✅ |
| Захват радио через FFmpeg-процесс | audio_capture.py | ✅ |
| Аппаратный выход (sounddevice) | audio_output.py | ✅ |
| HTTP-стрим в браузер (WAV) | main.py::/stream | ✅ |
| **DTMF-детектор** (метки типа 3957) | dtmf_detector.py | ✅ |
| CTCSS/DCS детекторы (задел на будущее) | tone_detector.py | ✅ |
| Микширование с fade in/out | engine.py::_apply_envelope | ✅ |
| Расписание объявлений (обяз., время/лимит/интервал) | announcement_store.py | ✅ |
| Ротация объявлений по last_played | engine.py::_pick_announcement_for_trigger | ✅ |
| Очередь с индикаторами приближения | engine.py::list_with_progress, app.js::renderQueue | ✅ |
| Fallback-источники при падении основного | engine.py::_stream_manager | ✅ |
| SIP/ЛСО (pjsua2) — звонок приостанавливает радио | sip_client.py | ✅ |
| Эквалайзер с лог-полосами + peak-hold | engine.py::_compute_spectrum | ✅ |
| Автозапуск движка/SIP | settings_store.py + main.py::_boot_autostart | ✅ |
| Модалки: Объявления/Настройки/SIP/О программе | static/app.js | ✅ |
| Ретро-стиль с колонками по бокам | static/style.css | ✅ |

## Ключевые решения (НЕ переделывать без причины)

1. **RLock вместо Lock** в `engine._lock` — защита от дедлоков при вложенных `_update_state` внутри `with self._lock`.
2. **`update_settings` возвращает snapshot** — не вызывает `get_settings()` (иначе повторный захват lock).
3. **`_check_dtmf_timeout` выносит `_update_state` за пределы `with self._lock`** — иначе дедлок.
4. **FFmpeg как долгоживущий subprocess** (не pydub для потоков) — устраняет «икание» звука.
5. **DTMF-метки хранятся в settings.json** — пользователь сам вводит, например 3957.
6. **Объявления без расписания запрещены** — обязательны `start_time` и (end_time или repeat_limit).
7. **`min_interval_minutes`** — минимальная пауза между показами одного файла (решает проблему «24 раза подряд»).
8. **`_set_event` с кулдауном 3 сек** — устраняет «прыгание» поля «Событие».
9. **Модалки вне `<div class="evraz-console">`** — иначе `position: fixed` ломается.
10. **PyInstaller: пути через `sys.executable`** для assets/ (settings.json, users.json, announcements/) — иначе в onefile всё теряется.

## Структура проекта

```
office_radio_overlay/
├── main.py                  # Flask-роуты, автозапуск, /api/*
├── engine.py                # Главный движок (RadioOverlayEngine)
├── audio_capture.py         # FFmpeg-процесс или sounddevice InputStream
├── audio_output.py          # HardwareOutput (sounddevice OutputStream)
├── dtmf_detector.py         # DTMF-детектор (FFT на 8 частот)
├── tone_detector.py         # CTCSS/DCS + FFT-спектр
├── mixer.py                 # load_announcement (pydub)
├── announcement_store.py    # CRUD объявлений + расписание + прогресс
├── settings_store.py        # settings.json (персистентность)
├── users_store.py           # users.json (хэши паролей werkzeug)
├── auth.py                  # @login_required, @admin_required
├── sip_client.py            # PJSIP-обёртка
├── config.py                # Все константы
├── office_radio_overlay.spec # PyInstaller
├── requirements.txt
├── README.md, LICENSE, .gitignore
├── templates/index.html     # ретро-панель
├── templates/login.html
├── templates/change_password.html
├── static/app.js            # Вся клиентская логика
├── static/style.css         # EVRAZ-стиль
├── static/favicon.ico
└── assets/                  # создаётся автоматически
    ├── settings.json        # НЕ в git (пароль SIP!)
    ├── users.json           # НЕ в git (хэши паролей!)
    ├── announcements.json   # НЕ в git
    └── announcements/       # НЕ в git (аудиофайлы)
```

## Известные проблемы / что не сделано

- [ ] **Сборка .exe:** PyInstaller собирается, но нет финального теста на «чистой» машине.
- [ ] **Тест DTMF в реальном эфире:** тест-кнопка работает, но реальная метка 3957 в эфире ещё не поймана.
- [ ] **SIP в реальной АТС:** подключение к sip2sip тестировалось, интеграция с офисной УАТС — нет.
- [ ] **Выбор аудиоустройства для SIP** через UI: сейчас PJSIP использует default device.
- [ ] **Плавный fade при SIP-звонке:** сейчас радио просто выключается на время звонка.
- [ ] **README:** нужно заменить плейсхолдеры `your-username` → `andrey-geben`.
- [ ] **Релиз GitHub:** v1.0.0 не создан, ZIP не приложен.

## План на v1.0 (ближайшее)

1. Протестировать текущий билд 15–20 минут (дедлоки, 500-е, зависания).
2. Собрать .exe: `pyinstaller --clean office_radio_overlay.spec`.
3. Скопировать `ffmpeg.exe` рядом с .exe (путь: `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_...\bin\ffmpeg.exe`).
4. Удалить `dist/office_radio_overlay/assets/` перед ZIP (иначе пароли утекут).
5. `Compress-Archive` → `office_radio_overlay_v1.0.0_windows_x64.zip`.
6. Тест .exe в чистой папке (assets/ создаётся рядом, FFmpeg находится).
7. `git push` всех правок.
8. GitHub Release v1.0.0 с прикреплённым ZIP.

## План на v1.1 (после релиза)

- Выбор аудиоустройства для SIP через UI.
- Fade-in/out при SIP-звонке (сейчас резкий обрыв).
- silencedetect как fallback-триггер (если DTMF не ловится — использовать паузу в эфире).
- История сработок (лог последних 100 событий с временем и результатом).
- Экспорт/импорт настроек (backup/restore JSON).
- Уведомления в Telegram при сработке DTMF/ошибках.

## Особенности UI

- **Стиль:** EVRAZ, ретро-проигрыватель, тёмный графит (#231F20), оранжевый (#EF7622), жёлтый (#FBAE18), красный (#EF4F24).
- **Колонки по бокам:** двухполосные (твитер + вуфер), бейдж EVRAZ.
- **Транспортная панель:** клавиши PLAY/STOP/ОБЪЯВЛЕНИЯ/НАСТРОЙКИ/SIP/О ПРОГРАММЕ.
- **Дашборд:** стрелочные индикаторы (SIGNAL/CPU/NET) + вертикальный столбик лампочек (CTCSS/DCS/ON AIR/SIP) + эквалайзер + гистограмма субтонов рядом.
- **Очередь в эфир:** вертикальные планки #1, #2... с LED-индикатором заполнения и подсказкой (готов / через 45м / после 18:00).
- **Модалки:** все настройки и списки в модальных окнах, открываются через транспорт.

## Важные детали для продолжения

- **При любых правках в engine.py** проверяйте, нет ли `_update_state()` внутри `with self._lock:` — это дедлок.
- **Логи сервера** (терминал `python main.py`) — главный источник диагностики. Там видны `POST /api/settings 200` или `ERROR in app: ...`.
- **F12 → Network** — второй инструмент: `(ожидает)` в запросах к `/api/status` = дедлок; `500` = ошибка на сервере; `304` = кэш app.js.
- **F12 → Console** — третье: `Сервер не отвечает` после 3-х ошибок = сервер упал или висит.
- **git push** — коммиты делает пользователь сам, я даю только команды. Никогда не коммить `assets/settings.json`, `assets/users.json`, `dist/`, `*.zip`, `*.exe`.

## Что хочу от ассистента в новых сессиях

1. Давать **полные файлы** при правках (не фрагменты) — чтобы не было рассинхрона версий.
2. **Указывать точное место вставки** для HTML/CSS: «найдите `X`, замените на `Y`».
3. Помнить: **Windows PowerShell**, не bash.
4. При предложении правки, затрагивающей threading, сразу проверять на дедлоки.
5. Не менять архитектуру (RLock, snapshot в update_settings, FFmpeg-subprocess) без явной просьбы.

## Первый запрос следующей сессии — образец

> Продолжаем Office Radio Overlay. Текущее состояние: v1.0 работает, собран в exe. Что сделано / что осталось — см. промт выше. Сегодня хочу [описание задачи].

---

**Готово, продолжаем с того места, где остановились.**