# Аналитика Mini App

Для продукта используется self-hosted [GoatCounter](https://github.com/arp242/goatcounter): лёгкая открытая аналитика без cookies и сторонних рекламных профилей. На VPS она работает отдельным systemd-сервисом с собственной SQLite-базой и доступна через nginx.

## Как смотреть

1. Откройте `https://stats.153-76-201-10.sslip.io`.
2. Войдите под адресом администратора, указанным при первоначальной настройке.
3. На главной странице смотрите посещения экранов. Они имеют адреса `/app/collections`, `/app/collection`, `/app/balance`, `/app/history`, `/app/profile`, `/app/welcome` и `/app/invitation`.
4. Действия отображаются как события. Основные: `event/collection-created`, `event/collection-joined`, `event/expense-added-equal`, `event/expense-added-custom`, `event/repayment-submitted`, `event/repayment-confirmed`, `event/repayment-rejected`, `event/transaction-edited`, `event/transaction-cancelled`, `event/invitation-shared`, `event/funds-requested`, `event/collection-archived` и `event/expense-statistics-opened`.
5. `event/app-session` показывает запуски в рамках аналитических сессий. События `event/active-user-<Telegram ID>` позволяют увидеть активные ID, а источник `telegram-user-<Telegram ID>` — связать экраны и действия с конкретным пользователем.

В аналитику передаётся только числовой Telegram ID. Имена, username, названия сборов, суммы, комментарии и платёжные реквизиты не передаются.

## Проверка на VPS

```bash
systemctl status goatcounter --no-pager
journalctl -u goatcounter -n 100 --no-pager
curl -I https://stats.153-76-201-10.sslip.io
```

База находится в `/var/lib/goatcounter/goatcounter.sqlite3`. Для резервной копии достаточно копировать этот файл вместе с основной базой приложения. Mini App отправляет статистику только если в `/opt/shakeonit/.env` задано:

```dotenv
ANALYTICS_URL=https://stats.153-76-201-10.sslip.io
```
