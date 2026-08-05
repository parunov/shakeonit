from __future__ import annotations

from html import escape

from .money import format_money


def user_label(row) -> str:
    username = f" (@{escape(row['username'])})" if row["username"] else ""
    return f"{escape(row['full_name'])}{username}"


async def collection_text(service, collection) -> str:
    snapshot = await service.collection_snapshot(collection["id"])
    participants = snapshot.participants
    balances = snapshot.balances
    debts = snapshot.debts
    names = {row["id"]: user_label(row) for row in participants}
    currency = collection["currency"]
    status = "активен" if collection["status"] == "active" else "в архиве"
    lines = [
        f"<b>🧾 {escape(collection['title'])}</b>",
        f"Валюта: <b>{currency}</b> · Участников: <b>{len(participants)}</b> · {status}",
        f"Всего затрат: <b>{format_money(snapshot.total, currency)}</b>",
        "",
        "<b>Нынешние балансы</b>",
    ]
    for row in participants:
        balance = balances[row["id"]]
        if balance > 0:
            state = f"должны {format_money(balance, currency)}"
        elif balance < 0:
            state = f"должен {format_money(-balance, currency)}"
        else:
            state = "расчет закрыт"
        lines.append(f"• {names[row['id']]} — {state}")
    lines.extend(["", "<b>Кто кому переводит</b>"])
    if debts:
        lines.extend(
            f"• {names[d.debtor_id]} → {names[d.creditor_id]}: "
            f"<b>{format_money(d.amount, currency)}</b>"
            for d in debts
        )
    else:
        lines.append("✅ Никто никому не должен")
    return "\n".join(lines)


def history_text(collection, rows, total: int, participants_count: int) -> str:
    currency = collection["currency"]
    expenses = [row for row in rows if row["kind"] == "expense"]
    repayments = [row for row in rows if row["kind"] == "repayment"]
    lines = [
        f"<b>📜 История · {escape(collection['title'])}</b>",
        f"Общая сумма затрат: <b>{format_money(total, currency)}</b>",
        f"Участников: <b>{participants_count}</b>",
        "",
        "<b>Затраты</b>",
    ]
    if not expenses:
        lines.append("— пока нет")
    for row in expenses:
        marker = "❌ отменена · " if row["status"] == "cancelled" else ""
        comment = f" · {escape(row['comment'])}" if row["comment"] else ""
        lines.append(
            f"{marker}{row['created_at'][:16]} · {escape(row['creator_name'])} · "
            f"<b>{format_money(row['amount'], currency)}</b>{comment}"
        )
    lines.extend(["", "<b>Возвраты долгов</b>"])
    if not repayments:
        lines.append("— пока нет")
    for row in repayments:
        marker = "❌ отменен · " if row["status"] == "cancelled" else ""
        lines.append(
            f"{marker}{row['created_at'][:16]} · {escape(row['creator_name'])} → "
            f"{escape(row['counterparty_name'])} · "
            f"<b>{format_money(row['amount'], currency)}</b>"
        )
    lines.append("\nНажмите транзакцию ниже, чтобы открыть детали.")
    return "\n".join(lines)


async def transaction_text(service, transaction) -> str:
    collection = await service.get_collection(transaction["collection_id"])
    rows = await service.history(collection["id"], 1000)
    row = next(item for item in rows if item["id"] == transaction["id"])
    kind = "Затрата" if row["kind"] == "expense" else "Возврат долга"
    lines = [
        f"<b>{kind} #{row['id']}</b>",
        f"Дата: {row['created_at'][:16]}",
        f"Инициатор: {escape(row['creator_name'])}",
        f"Сумма: <b>{format_money(row['amount'], collection['currency'])}</b>",
    ]
    if row["kind"] == "expense":
        lines.append(f"Распределено на: {escape(row['shared_with'] or '—')}")
    else:
        lines.append(f"Получатель: {escape(row['counterparty_name'])}")
    if row["comment"]:
        lines.append(f"Комментарий: {escape(row['comment'])}")
    if row["status"] == "cancelled":
        lines.append("\n❌ Транзакция отменена и не влияет на баланс.")
    return "\n".join(lines)
