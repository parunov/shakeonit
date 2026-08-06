from __future__ import annotations

from collections.abc import Mapping
from html import escape

from .money import format_money


def telegram_user_link(user_id: int, full_name: str, username: str | None = None) -> str:
    label = escape(full_name)
    if username:
        label += f" (@{escape(username)})"
    return f'<a href="tg://user?id={int(user_id)}">{label}</a>'


def user_label(row) -> str:
    return telegram_user_link(row["id"], row["full_name"], row["username"])


def transaction_update_report(
    actor_name: str,
    collection,
    before,
    after,
    before_participants=(),
    after_participants=(),
    *,
    actor_id: int | None = None,
    actor_username: str | None = None,
) -> str:
    """Render a concrete audit message without exposing an internal transaction id."""

    def state_text(transaction, participant_names) -> str:
        comment = escape(transaction["comment"]) if transaction["comment"] else "без комментария"
        parts = [f"<b>{format_money(transaction['amount'], collection['currency'])}</b>"]
        parts.append(f"комментарий: {comment}")
        if transaction["kind"] == "expense":
            names = (
                ", ".join(
                    telegram_user_link(item["user_id"], item["full_name"], item.get("username"))
                    if isinstance(item, Mapping)
                    else escape(str(item))
                    for item in participant_names
                )
                or "не выбраны"
            )
            parts.append(f"участники: {names}")
        return " · ".join(parts)

    kind = "затрату" if before["kind"] == "expense" else "возврат долга"
    actor = (
        telegram_user_link(actor_id, actor_name, actor_username)
        if actor_id is not None
        else escape(actor_name)
    )
    return (
        f"✏️ {actor} изменил(а) {kind} по сбору "
        f"<b>«{escape(collection['title'])}»</b>.\n"
        f"Было: {state_text(before, before_participants)}\n"
        f"Стало: {state_text(after, after_participants)}"
    )


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
        f"Валюта: <b>{currency}</b> · Участников: "
        f"<b>{sum(bool(row['active']) for row in participants)}</b> · {status}",
        f"Всего затрат: <b>{format_money(snapshot.total, currency)}</b>",
        "",
        "<b>Нынешние балансы</b>",
    ]
    for row in participants:
        balance = balances[row["id"]]
        if balance > 0:
            state = f"должны {format_money(balance, currency)}"
        elif balance < 0:
            state = f"должен(а) {format_money(-balance, currency)}"
        else:
            state = "расчет закрыт"
        former = " · вышел(ла) из сбора" if not row["active"] else ""
        lines.append(f"• {names[row['id']]}{former} — {state}")
    lines.extend(["", "<b>Кто кому переводит</b>"])
    if debts:
        lines.extend(
            f"• {names[d.debtor_id]} → {names[d.creditor_id]}: "
            f"<b>{format_money(d.amount, currency)}</b>"
            for d in debts
        )
    else:
        lines.append("✅ Никто никому не должен(а)")
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
            f"{marker}{row['created_at'][:16]} · "
            f"{telegram_user_link(row['creator_id'], row['creator_name'], row['creator_username'])} · "
            f"<b>{format_money(row['amount'], currency)}</b>{comment}"
        )
    lines.extend(["", "<b>Возвраты долгов</b>"])
    if not repayments:
        lines.append("— пока нет")
    for row in repayments:
        if row["status"] == "cancelled":
            marker = "❌ отменен · "
        elif row["confirmation_status"] == "pending":
            marker = "⏳ ожидает подтверждения · "
        else:
            marker = "✅ подтвержден · "
        lines.append(
            f"{marker}{row['created_at'][:16]} · "
            f"{telegram_user_link(row['creator_id'], row['creator_name'], row['creator_username'])} → "
            f"{telegram_user_link(row['counterparty_id'], row['counterparty_name'], row['counterparty_username'])} · "
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
        f"Инициатор: {telegram_user_link(row['creator_id'], row['creator_name'], row['creator_username'])}",
        f"Сумма: <b>{format_money(row['amount'], collection['currency'])}</b>",
    ]
    if row["kind"] == "expense":
        shares = (await service.expense_shares_for_transactions([row["id"]])).get(row["id"], [])
        lines.append("<b>Распределение:</b>")
        lines.extend(
            f"• {telegram_user_link(share['user_id'], share['full_name'], share['username'])} — "
            f"{format_money(share['amount'], collection['currency'])}"
            for share in shares
        )
    else:
        lines.append(
            "Получатель: "
            f"{telegram_user_link(row['counterparty_id'], row['counterparty_name'], row['counterparty_username'])}"
        )
        if row["confirmation_status"] == "pending" and row["status"] == "active":
            lines.append("Статус: <b>⏳ ожидает подтверждения получателем</b>")
        elif row["confirmation_status"] == "confirmed":
            lines.append("Статус: <b>✅ получение подтверждено</b>")
    if row["comment"]:
        lines.append(f"Комментарий: {escape(row['comment'])}")
    if row["status"] == "cancelled":
        if row["kind"] == "repayment" and row["cancelled_by"] == row["counterparty_id"]:
            lines.append("\n❌ Получение отклонено. Возврат не влияет на баланс.")
        else:
            lines.append("\n❌ Транзакция отменена и не влияет на баланс.")
    return "\n".join(lines)
