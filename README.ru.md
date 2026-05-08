# Dataclenear / Presidio RU для data.zed.md

Этот fork разворачивает Microsoft Presidio как русскоязычный сервис для поиска и обезличивания персональных данных.

## Что включено

- Русский UI для загрузки документа, подсветки найденных фрагментов и скачивания финального текста.
- Синхронный просмотр оригинала и обезличенного результата слева/справа.
- Навигация стрелками по найденным заменам.
- Таблица PII-замен с ручным переопределением значения замены.
- Таблица фактов и утверждений с ручной или автоматической заменой.
- Backend processor для TXT/MD/CSV/JSON/LOG/PDF/DOCX и durable artifacts в S3-compatible storage.
- Presidio Analyzer на `/analyzer/*`.
- Presidio Anonymizer на `/anonymizer/*`.
- Конфигурация Analyzer для `ru` и `en`.
- Stanza NLP model для русского языка.
- Pattern-recognizers для типовых российских идентификаторов: телефон, email, банковская карта, паспорт РФ, ИНН, СНИЛС.

## Быстрый smoke

```bash
curl -fsS https://data.zed.md/health
curl -fsS https://data.zed.md/analyzer/health
curl -fsS https://data.zed.md/anonymizer/health
```

Русский анализ:

```bash
curl -fsS https://data.zed.md/analyzer/analyze \
  -H 'Content-Type: application/json' \
  -d '{"text":"Иван Петров, паспорт 4510 123456, ИНН 7707083893, телефон +7 916 123-45-67, email ivan@example.ru","language":"ru"}'
```

Обезличивание:

```bash
curl -fsS https://data.zed.md/anonymizer/anonymize \
  -H 'Content-Type: application/json' \
  -d '{"text":"Иван Петров, телефон +7 916 123-45-67","analyzer_results":[{"start":21,"end":37,"entity_type":"RU_PHONE_NUMBER","score":1.0}]}'
```

## Деплой

Основной compose overlay находится в `deploy/data-zed-md/docker-compose.yml`.
Для локального запуска есть безопасные MinIO defaults. Для постоянного окружения скопируйте
`deploy/data-zed-md/processor.env.example` в `deploy/data-zed-md/processor.env` и замените
значения вне git.

На сервере:

```bash
docker compose -f deploy/data-zed-md/docker-compose.yml up -d --build
```

Gateway слушает `127.0.0.1:18088`. Для публичного домена Caddy должен проксировать `data.zed.md` на этот локальный порт.
