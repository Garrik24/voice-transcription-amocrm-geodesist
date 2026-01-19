## Geodesist notification (MAX via Wappi)

Автоматизация для AmoCRM: при переводе сделки на этап **«Назначен»** отправляет геодезисту сообщение в **MAX** через Wappi.

### Вход
Webhook от робота AmoCRM в сервис (см. `PLAN.md`):
- `lead_id` (обязательно)
- `geodesist` (опционально: "1"/"2") или `geodesist_phone`

### Выход
- Сообщение геодезисту в MAX (через Wappi MAX API)
- Примечание в сделке AmoCRM о факте отправки

