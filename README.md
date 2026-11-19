# ДЗ 2. Marketplace: OpenAPI + CRUD

FastAPI, PostgreSQL 18 и Flyway 11. Контракт хранится в [contract/openapi.json](contract/openapi.json). Из него при сборке генерируются Pydantic DTO и FastAPI-маршруты; бизнес-операции находятся в [app/service.py](app/service.py).

## Запуск и проверка

```bash
docker compose up --build -d
docker compose exec -T api python scripts/demo.py
docker compose exec -T api python -m unittest discover -s tests -v
```

API: `http://localhost:8012`, Swagger UI: `http://localhost:8012/docs`. В Swagger сначала выполните регистрацию и вход, затем укажите access token в **Authorize**.

Миграция создаёт демонстрационного администратора: `admin` / `admin-demo-password`. Публичная регистрация разрешает `USER` и `SELLER`; назначить себе `ADMIN` через API нельзя. Access token действует 15 минут, refresh token — 7 дней. `/auth/refresh` заменяет refresh token, повторное использование старого запрещено.

Демонстрация проходит через HTTP, сгенерированные маршруты и PostgreSQL: создаёт продавца и покупателя, товар и промокод; резервирует остатки, обновляет заказ со снятием скидки и отменяет его. Данные остаются в БД для проверки:

```bash
docker compose exec -T db psql -U marketplace -d marketplace -c "SELECT id, name, price, stock, status, seller_id FROM products ORDER BY created_at DESC LIMIT 5;"
docker compose exec -T db psql -U marketplace -d marketplace -c "SELECT id, user_id, status, total_amount, discount_amount, promo_code_id FROM orders ORDER BY created_at DESC LIMIT 5;"
docker compose exec -T db psql -U marketplace -d marketplace -c "SELECT order_id, product_id, quantity, price_at_order FROM order_items LIMIT 5;"
docker compose exec -T db psql -U marketplace -d marketplace -c "SELECT code, current_uses, max_uses FROM promo_codes ORDER BY code LIMIT 5;"
docker compose exec -T db psql -U marketplace -d marketplace -c "SELECT user_id, operation_type, created_at FROM user_operations ORDER BY created_at DESC LIMIT 5;"
docker compose exec -T db psql -U marketplace -d marketplace -c "SELECT version, description, success FROM flyway_schema_history;"
```

## Контракт и генерация

Генерация отдельно от запуска: `python scripts/generate.py` в Python 3.12 с установленным `requirements.txt`, либо одна команда сборки `docker compose build api`. Каталог `app/generated/` игнорируется Git и Docker-контекстом; при сборке он создаётся заново.

DTO создаёт `datamodel-code-generator`. Генератор маршрутов читает `operationId`, схемы, параметры и роли из того же контракта: ручных DTO и ручной регистрации бизнес-маршрутов нет. [app/serialization.py](app/serialization.py) задаёт общую сериализацию `Decimal` в JSON number. До бизнес-логики исходный JSON проверяется по ограничениям контракта, включая строгие типы без преобразования `true` или строки в число.

## Поведение

- Товары: полный CRUD, точные фильтры `status`/`category`, `page` с нуля, `size=20` по умолчанию. Удаление переводит товар в `ARCHIVED`. Продавец изменяет только свои товары, администратор — любые; при создании он может указать существующего продавца в `seller_id`.
- Заказы: остатки, снимки цен, скидка и запись операции изменяются одной транзакцией. Строки товаров блокируются в порядке ID; проверка лимита и активного заказа сериализуется по пользователю. При ошибке откатываются все изменения.
- Обновление допускается только для `CREATED`, отмена — для `CREATED` и `PAYMENT_PENDING`. Для прохождения остальных состояний добавлен один административный маршрут `POST /orders/{id}/status`; отмена выполняется отдельно через `/cancel`.
- Лимит создания и обновления считается независимо по успешным операциям; интервал задаёт `ORDER_LIMIT_MINUTES`, по умолчанию 1 минута. При создании проверка лимита предшествует проверке активного заказа, как в условии.
- Одинаковые товары в нескольких позициях объединяются перед проверкой остатка. При обновлении используются текущие цены новых позиций; уже созданный заказ сам по себе от изменения цены товара не меняется.
- У применённого промокода при обновлении заново проверяются срок, активность и минимальная сумма. Если условия больше не выполнены, скидка снимается и использование освобождается. Уже занятое этим заказом использование не считается новым; `current_uses` повторно не увеличивается.
- Фраза условия «скидка не более 70%, иначе цена 100%» трактуется буквально: при проценте свыше 70 скидка равна нулю. Иначе она округляется вниз до копейки; фиксированная скидка ограничена суммой заказа.

Все ошибки имеют `error_code`, `message`, `details`; ошибки входа возвращают 400 с полями нарушений. Каждый запрос записывается в JSON с `request_id`; пароль и токены в теле маскируются. Идентификатор запроса возвращается в `X-Request-Id`.

Интеграционные тесты используют настоящий PostgreSQL. Они проверяют контрактные границы, роли, снимок цены, откаты создания/обновления, промокоды, состояния, возврат остатков, лимиты, JWT/refresh, логирование и одновременную борьбу за последний товар/последнее использование промокода.
