from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .money import CURRENCIES


def main_menu() -> ReplyKeyboardMarkup:
    app = KeyboardButton(text="Открыть приложение")
    return ReplyKeyboardMarkup(
        keyboard=[[app]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Открыть приложение «По рукам»",
    )


def webapp_launch(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📱 Открыть приложение",
                    web_app=WebAppInfo(url=url),
                )
            ]
        ]
    )


def currencies() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for currency in CURRENCIES:
        builder.button(text=currency, callback_data=f"currency:{currency}")
    builder.adjust(2)
    return builder.as_markup()


def collections_keyboard(rows, action: str = "open") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        marker = "📦 " if row["status"] == "archived" else ""
        marker += "✅ " if row["is_participant"] else "➕ "
        builder.button(
            text=f"{marker}{row['title']} · {row['currency']}",
            callback_data=f"{action}:{row['id']}",
        )
    builder.adjust(1)
    return builder.as_markup()


def collection_actions(
    collection,
    is_member: bool,
    is_admin: bool,
    start_url: str | None = None,
    app_url: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    collection_id = collection["id"]
    if collection["status"] == "active":
        if app_url:
            builder.button(text="📱 Открыть приложение", url=app_url)
        if start_url:
            builder.button(text="🚀 Начать и участвовать", url=start_url)
        builder.button(text="🙋 Участвовать в сборе", callback_data=f"join:{collection_id}")
    if is_member:
        builder.button(text="💸 Добавить затрату", callback_data=f"expense:{collection_id}")
        builder.button(text="🤝 Вернуть долг", callback_data=f"repay:{collection_id}")
        builder.button(text="📜 История", callback_data=f"history:{collection_id}:0")
        builder.button(text="👥 Участники", callback_data=f"members:{collection_id}")
        builder.button(text="⚖️ Мой баланс", callback_data=f"mybalance:{collection_id}")
    if is_admin and collection["status"] == "active":
        builder.button(text="⚙️ Управление · админ", callback_data=f"manage:{collection_id}")
    if is_admin and collection["status"] == "archived":
        builder.button(text="♻️ Восстановить", callback_data=f"restore:{collection_id}")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def participant_picker(rows, selected: set[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        mark = "✅" if row["id"] in selected else "◻️"
        builder.button(text=f"{mark} {row['full_name']}", callback_data=f"share:{row['id']}")
    builder.button(text="Выбрать всех", callback_data="share:all")
    builder.button(text="Готово", callback_data="share:done")
    builder.adjust(1)
    return builder.as_markup()


def people_keyboard(rows, action: str, exclude_id: int | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        if row["id"] != exclude_id:
            builder.button(text=row["full_name"], callback_data=f"{action}:{row['id']}")
    builder.adjust(1)
    return builder.as_markup()


def history_keyboard(collection_id: int, rows, offset: int, can_next: bool):
    builder = InlineKeyboardBuilder()
    for row in rows:
        icon = "💸" if row["kind"] == "expense" else "🤝"
        cancelled = "❌ " if row["status"] == "cancelled" else ""
        builder.button(
            text=f"{cancelled}{icon} #{row['id']} · {row['creator_name']}",
            callback_data=f"tx:{row['id']}",
        )
    navigation: list[InlineKeyboardButton] = []
    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                text="← Назад", callback_data=f"history:{collection_id}:{max(0, offset - 10)}"
            )
        )
    if can_next:
        navigation.append(
            InlineKeyboardButton(
                text="Дальше →", callback_data=f"history:{collection_id}:{offset + 10}"
            )
        )
    if navigation:
        builder.row(*navigation)
    builder.row(InlineKeyboardButton(text="К сбору", callback_data=f"open:{collection_id}"))
    builder.adjust(1)
    return builder.as_markup()


def transaction_actions(transaction, collection, actor_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    can_respond = (
        transaction["kind"] == "repayment"
        and transaction["status"] == "active"
        and transaction["confirmation_status"] == "pending"
        and actor_id == transaction["counterparty_id"]
    )
    if can_respond:
        builder.button(
            text="✅ Подтвердить получение",
            callback_data=f"repayconfirm:{transaction['id']}",
        )
        builder.button(
            text="❌ Отклонить",
            callback_data=f"repayreject:{transaction['id']}",
        )
    if (
        not can_respond
        and transaction["status"] == "active"
        and collection["status"] == "active"
        and actor_id in (transaction["creator_id"], collection["admin_id"])
        and not (
            transaction["kind"] == "repayment"
            and transaction["confirmation_status"] == "confirmed"
            and actor_id != collection["admin_id"]
        )
    ):
        if not (
            transaction["kind"] == "repayment" and transaction["confirmation_status"] == "confirmed"
        ):
            builder.button(text="✏️ Изменить", callback_data=f"txedit:{transaction['id']}")
        builder.button(text="❌ Отменить", callback_data=f"txcancel:{transaction['id']}")
    builder.button(text="← В историю", callback_data=f"history:{collection['id']}:0")
    builder.adjust(2, 1)
    return builder.as_markup()


def admin_actions(collection_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👑 Передать администратора", callback_data=f"transfer:{collection_id}")
    builder.button(text="🗑 Удалить участника", callback_data=f"remove:{collection_id}")
    builder.button(text="📦 Завершить и архивировать", callback_data=f"archive:{collection_id}")
    builder.button(text="← К сбору", callback_data=f"open:{collection_id}")
    builder.adjust(1)
    return builder.as_markup()


def confirmation(
    action: str, entity_id: int, cancel_callback: str | None = None
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data=f"{action}yes:{entity_id}"),
                InlineKeyboardButton(
                    text="Нет", callback_data=cancel_callback or f"open:{entity_id}"
                ),
            ]
        ]
    )
