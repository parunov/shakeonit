from __future__ import annotations

import re
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    WebAppInfo,
)

from .config import Settings
from .keyboards import (
    admin_actions,
    collection_actions,
    collections_keyboard,
    confirmation,
    currencies,
    history_keyboard,
    main_menu,
    participant_picker,
    people_keyboard,
    transaction_actions,
    webapp_launch,
)
from .links import group_start_param
from .money import format_money, parse_amount
from .notifications import notify_subscribers
from .render import collection_text, history_text, transaction_text, user_label
from .service import BudgetService, DomainError
from .states import AddExpense, AddRepayment, CreateCollection, EditTransaction, PaymentDetails

router = Router()

HELP_TEXT = """<b>❓ Помощь</b>

<b>Сбор</b> — отдельный бюджет события: поездки, праздника или общего подарка. Каждый сбор имеет одну валюту.

<b>Затрата</b> — один участник заплатил, а сумма делится поровну между выбранными людьми. Плательщика тоже можно выбрать.

<b>Вернуть долг</b> — фактический перевод от одного участника другому. Баланс изменится, когда получатель подтвердит деньги.

На экране сбора всегда показаны чистые балансы и оптимальный список переводов. Отмена транзакции сохраняет ее в истории, но исключает из расчета.

Кнопка «📱 Приложение» открывает тот же функционал в удобном Mini App.

Команды: /menu, /new, /collections, /expense, /repay, /balance, /help, /cancel"""

TUTORIAL_TEXT = """<b>🎓 Как пользоваться ShareBudget</b>

1. Добавьте бота в Telegram-группу и нажмите «Создать сбор».
2. Назовите событие и выберите BYN, RUB, EUR или USD.
3. Новый пользователь нажимает «Участвовать в сборе» прямо в группе. Telegram ID регистрируется автоматически, без личного чата и отдельного входа.
4. После подключения участника можно выбрать при добавлении затраты.
5. Тот, кто заплатил, нажимает «Добавить затрату», вводит сумму, отмечает людей и пишет короткое описание.
6. Когда кто-то действительно переводит деньги, используйте «Вернуть долг».
7. Экран сбора сразу покажет, кто кому и сколько должен. В «Истории» видны все действия.
8. Основная работа проходит в Mini App: оно открывается прямо из группы и показывает сборы этой группы.

Быстрая запись при включенном Privacy Mode: <code>/expense@имя_бота 40 @ivan @maxim билеты</code>. Если сборов несколько, бот предложит выбрать нужный. Отмеченные пользователи должны участвовать в сборе.

Администратор может отменять и редактировать любые транзакции, удалять неиспользованных участников, передавать роль и завершать сбор. Восстановить сбор из архива можно 30 дней."""

MENTION_HINT = """⚡ <b>Быстрая затрата в ShakeOnIt</b>

Отправьте в группе:
<code>/expense@ShakeOnIt_bot 40 @ivan @maxim билеты</code>

Остальные действия — сборы, балансы, возвраты и история — удобнее выполнять в Mini App.
Privacy Mode остается включённым."""


async def sync_user(service: BudgetService, event: Message | CallbackQuery) -> None:
    user = event.from_user
    chat = (
        event.chat if isinstance(event, Message) else event.message.chat if event.message else None
    )
    await service.upsert_user(
        user.id,
        user.username,
        user.full_name,
        private_started=bool(chat and chat.type == ChatType.PRIVATE),
    )


async def safe_edit(message: Message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def collection_markup(
    message: Message,
    collection,
    is_member: bool,
    is_admin: bool,
) -> InlineKeyboardMarkup:
    app_url = None
    if collection["status"] == "active" and message.chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        bot_user = await message.bot.get_me()
        app_url = (
            f"https://t.me/{bot_user.username}?startapp=collection_{collection['id']}&mode=compact"
            if bot_user.has_main_web_app
            else f"https://t.me/{bot_user.username}?start=app"
        )
    shared_group_screen = message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    if shared_group_screen:
        return collection_actions(collection, False, False, None, app_url)
    return collection_actions(
        collection,
        is_member,
        is_admin,
        None,
        app_url,
    )


async def show_collection(message: Message, collection_id: int, actor_id: int, service):
    collection = await service.get_collection(collection_id)
    if not collection:
        raise DomainError("Сбор не найден")
    is_member = await service.is_participant(collection_id, actor_id)
    text = await collection_text(service, collection)
    keyboard = await collection_markup(
        message, collection, is_member, collection["admin_id"] == actor_id
    )
    await safe_edit(message, text, keyboard)


@router.message(CommandStart())
async def start(
    message: Message,
    command: CommandObject,
    service: BudgetService,
    settings: Settings,
) -> None:
    await sync_user(service, message)
    private_menu = main_menu(settings.webapp_url if message.chat.type == ChatType.PRIVATE else None)
    if command.args == "app" and message.chat.type == ChatType.PRIVATE and settings.webapp_url:
        await message.answer(
            "✅ <b>ShakeOnIt готов</b>\n\nОткройте приложение — вход уже подтвержден Telegram.",
            reply_markup=webapp_launch(settings.webapp_url),
            parse_mode=ParseMode.HTML,
        )
        return
    match = re.fullmatch(r"collection_(\d+)", command.args or "")
    if match and message.chat.type == ChatType.PRIVATE:
        collection = await service.get_collection(int(match.group(1)))
        if not collection or collection["status"] != "active":
            await message.answer(
                "ℹ️ Сбор из приглашения не найден или уже завершен.",
                reply_markup=private_menu,
            )
            return
        was_member = await service.is_participant(collection["id"], message.from_user.id)
        if not was_member:
            await service.join(collection["id"], message.from_user.id, subscribe=True)
        else:
            await service.set_notification_subscription(
                collection["id"], message.from_user.id, True
            )
        await message.answer(
            (
                "✅ <b>Подключение завершено</b>\n\n"
                f"Вы {'уже участвовали' if was_member else 'теперь участвуете'} в сборе "
                f"«{escape(collection['title'])}». Бот запомнил ваш Telegram ID.\n\n"
                "🔔 Личные уведомления по этому сбору включены."
            ),
            reply_markup=private_menu,
            parse_mode=ParseMode.HTML,
        )
        await message.answer(
            await collection_text(service, collection),
            reply_markup=await collection_markup(
                message,
                collection,
                True,
                collection["admin_id"] == message.from_user.id,
            ),
            parse_mode=ParseMode.HTML,
        )
        if settings.webapp_url:
            await message.answer(
                "📱 Откройте сбор в приложении:",
                reply_markup=webapp_launch(settings.webapp_url),
            )
        return
    await message.answer(
        "👋 <b>Добро пожаловать в ShakeOnIt</b>\n\n"
        "Здесь не нужны логин, пароль или отдельная регистрация — Telegram уже безопасно "
        "подтвердил ваш профиль.\n\n"
        "📱 Все сборы, балансы, возвраты и история находятся в приложении.\n"
        "⚡ Для быстрой затраты в группе используйте: "
        "<code>/expense 40 @ivan @maxim билеты</code>.",
        reply_markup=private_menu,
        parse_mode=ParseMode.HTML,
    )
    if settings.webapp_url and message.chat.type == ChatType.PRIVATE:
        await message.answer(
            "📱 Или откройте полный интерфейс ShakeOnIt:",
            reply_markup=webapp_launch(settings.webapp_url),
        )


@router.message(Command("menu"))
@router.message(F.text == "🏠 Главное меню")
async def menu(
    message: Message, state: FSMContext, service: BudgetService, settings: Settings
) -> None:
    await sync_user(service, message)
    await state.clear()
    await message.answer(
        "Что хотите сделать?",
        reply_markup=main_menu(
            settings.webapp_url if message.chat.type == ChatType.PRIVATE else None
        ),
    )


@router.message(Command("cancel"))
async def cancel_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu())


@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
async def help_screen(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu())


@router.message(F.text == "🎓 Обучение")
async def tutorial(message: Message) -> None:
    await message.answer(TUTORIAL_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu())


@router.message(Command("app"))
@router.message(F.text.in_({"📱 Приложение", "📱 Открыть приложение"}))
async def open_webapp(message: Message, settings: Settings) -> None:
    if not settings.webapp_url:
        await message.answer("ℹ️ Приложение пока не настроено.")
        return
    if message.chat.type != ChatType.PRIVATE:
        bot_user = await message.bot.get_me()
        if bot_user.has_main_web_app:
            chat_param = group_start_param(message.chat.id, settings.bot_token)
            app_url = f"https://t.me/{bot_user.username}?startapp={chat_param}&mode=compact"
            text = "📱 Откройте сборы этой группы прямо в ShakeOnIt."
            button_text = "📱 Открыть приложение"
        else:
            app_url = f"https://t.me/{bot_user.username}?start=app"
            text = "📱 Перейдите в личный чат и откройте защищённую кнопку ShakeOnIt."
            button_text = "📱 Перейти к приложению"
        await message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=button_text, url=app_url)],
                ]
            ),
        )
        return
    await message.answer(
        "📱 <b>ShakeOnIt</b> — все сборы и операции в одном спокойном интерфейсе.",
        reply_markup=webapp_launch(settings.webapp_url),
        parse_mode=ParseMode.HTML,
    )


@router.message(F.chat_shared)
async def remember_shared_chat(message: Message, service: BudgetService) -> None:
    await sync_user(service, message)
    shared = message.chat_shared
    await service.register_user_chat(
        message.from_user.id,
        shared.chat_id,
        shared.title or shared.username or "Telegram-группа",
    )
    await message.answer(
        f"✅ Группа «{escape(shared.title or 'Telegram-группа')}» добавлена в приложение.",
        reply_markup=main_menu(),
    )


@router.my_chat_member()
async def remember_group_when_bot_is_added(
    event: ChatMemberUpdated, service: BudgetService
) -> None:
    if event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if event.new_chat_member.status not in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    ):
        return
    await service.upsert_user(
        event.from_user.id,
        event.from_user.username,
        event.from_user.full_name,
        private_started=False,
    )
    await service.register_user_chat(
        event.from_user.id,
        event.chat.id,
        event.chat.title or "Telegram-группа",
    )
    await event.bot.send_message(
        event.chat.id,
        "👋 <b>ShakeOnIt добавлен</b>\n\nГруппа готова. Откройте Mini App или "
        "используйте /new, чтобы создать первый сбор.",
        reply_markup=main_menu(),
    )


@router.message(Command("new"))
@router.message(F.text == "➕ Создать сбор")
async def new_collection(
    message: Message,
    state: FSMContext,
    service: BudgetService,
    settings: Settings,
) -> None:
    await sync_user(service, message)
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        if settings.webapp_url:
            bot_user = await message.bot.get_me()
            separator = "&" if "?" in settings.webapp_url else "?"
            create_url = f"{settings.webapp_url}{separator}intent=create"
            await message.answer(
                "➕ <b>Новый сбор</b>\n\nОткройте форму создания. Сбор можно вести "
                "в Telegram-группе или без группы — тогда уведомления будут приходить лично от бота.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="➕ Открыть форму создания",
                                web_app=WebAppInfo(url=create_url),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="👥 Добавить бота в группу",
                                url=f"https://t.me/{bot_user.username}?startgroup=shakeonit",
                            )
                        ],
                    ]
                ),
            )
            return
        await message.answer(
            "Создавать сбор нужно в общей Telegram-группе. Добавьте туда бота и повторите команду."
        )
        return
    await state.set_state(CreateCollection.title)
    await state.update_data(chat_id=message.chat.id)
    await message.answer(
        "Как назовем сбор? Например: <b>Поездка в Берлин</b>\n\n"
        "Ответьте прямо на это сообщение — так бот увидит название при включённом Privacy Mode.",
        parse_mode="HTML",
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="Название сбора",
        ),
    )


@router.message(CreateCollection.title)
async def collection_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not 2 <= len(title) <= 80:
        await message.answer("Название должно содержать от 2 до 80 символов. Попробуйте еще раз.")
        return
    await state.update_data(title=title)
    await state.set_state(CreateCollection.currency)
    await message.answer("Выберите единую валюту этого сбора:", reply_markup=currencies())


@router.callback_query(CreateCollection.currency, F.data.startswith("currency:"))
async def collection_currency(
    callback: CallbackQuery, state: FSMContext, service: BudgetService
) -> None:
    await sync_user(service, callback)
    data = await state.get_data()
    currency = callback.data.split(":", 1)[1]
    collection_id = await service.create_collection(
        data["chat_id"], data["title"], currency, callback.from_user.id
    )
    await state.clear()
    await callback.answer("Сбор создан")
    collection = await service.get_collection(collection_id)
    await callback.message.edit_text(
        "✅ <b>Сбор создан</b>\n\n" + await collection_text(service, collection),
        reply_markup=await collection_markup(callback.message, collection, True, True),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("collections"))
@router.message(F.text == "📋 Сборы")
@router.message(F.text == "📋 Мои сборы")
async def collections(message: Message, service: BudgetService) -> None:
    await sync_user(service, message)
    chat_id = (
        message.chat.id if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) else None
    )
    rows = await service.list_visible_collections(message.from_user.id, chat_id)
    if not rows:
        await message.answer("Доступных сборов пока нет.")
        return
    title = (
        "Активные сборы этой группы и сборы, в которых вы участвуете:\n"
        "✅ участвуете · ➕ можно вступить · 📦 архив"
        if chat_id is not None
        else "Сборы, в которых вы участвуете:"
    )
    await message.answer(title, reply_markup=collections_keyboard(rows))


@router.callback_query(F.data.startswith("open:"))
async def open_collection(callback: CallbackQuery, service: BudgetService) -> None:
    await callback.answer("⏳ Открываю сбор…")
    await sync_user(service, callback)
    await show_collection(
        callback.message, int(callback.data.split(":")[1]), callback.from_user.id, service
    )


@router.callback_query(F.data.startswith("join:"))
async def join_collection(callback: CallbackQuery, service: BudgetService) -> None:
    await sync_user(service, callback)
    collection_id = int(callback.data.split(":")[1])
    was_member = await service.is_participant(collection_id, callback.from_user.id)
    subscribe = callback.message.chat.type == ChatType.PRIVATE
    await service.join(collection_id, callback.from_user.id, subscribe=subscribe)
    collection = await service.get_collection(collection_id)
    await notify_subscribers(
        callback.bot,
        service,
        collection,
        f"🙋 {escape(callback.from_user.full_name)} участвует в сборе.",
        exclude_user_ids={callback.from_user.id},
    )
    await callback.answer(
        "✅ Вы уже участвуете в сборе."
        if was_member
        else "🎉 Готово! Вы участвуете — без регистрации и переходов.",
        show_alert=True,
    )
    await show_collection(callback.message, collection_id, callback.from_user.id, service)


async def choose_collection(message: Message, service, action: str, empty_text: str) -> None:
    chat_id = (
        message.chat.id if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) else None
    )
    rows = await service.list_collections(message.from_user.id, chat_id, include_archived=False)
    if not rows:
        await message.answer(empty_text)
    elif len(rows) == 1:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=rows[0]["title"], callback_data=f"{action}:{rows[0]['id']}"
                    )
                ]
            ]
        )
        await message.answer("Выберите сбор:", reply_markup=keyboard)
    else:
        await message.answer("Выберите сбор:", reply_markup=collections_keyboard(rows, action))


@router.message(Command("expense"))
@router.message(F.text.in_({"💸 Добавить затрату", "💸 Добавить трату"}))
async def expense_start(message: Message, state: FSMContext, service: BudgetService) -> None:
    await sync_user(service, message)
    await state.clear()
    command = re.fullmatch(r"/expense(?:@[A-Za-z0-9_]+)?(?:\s+(.+))?", (message.text or "").strip())
    if command and command.group(1):
        await quick_expense(message, command.group(1), service, state)
        return
    await choose_collection(
        message, service, "expense", "Нет активных сборов для добавления затраты."
    )


@router.callback_query(F.data.startswith("expense:"))
async def expense_collection(
    callback: CallbackQuery, state: FSMContext, service: BudgetService
) -> None:
    await sync_user(service, callback)
    collection_id = int(callback.data.split(":")[1])
    if not await service.is_participant(collection_id, callback.from_user.id):
        raise DomainError("Сначала вступите в сбор")
    await state.set_state(AddExpense.amount)
    await state.update_data(collection_id=collection_id)
    await callback.answer("✨ Добавим новую затрату")
    await callback.message.answer(
        "Введите сумму затраты, например <b>120,50</b>:", parse_mode="HTML"
    )


@router.message(AddExpense.amount)
async def expense_amount(message: Message, state: FSMContext, service: BudgetService) -> None:
    try:
        amount = parse_amount(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    rows = await service.list_participants(data["collection_id"])
    await state.update_data(amount=amount, selected=[])
    await state.set_state(AddExpense.participants)
    await message.answer(
        "На кого разделить эту сумму? Можно отметить и самого плательщика.",
        reply_markup=participant_picker(rows, set()),
    )


@router.callback_query(AddExpense.participants, F.data.startswith("share:"))
async def expense_participants(
    callback: CallbackQuery, state: FSMContext, service: BudgetService
) -> None:
    data = await state.get_data()
    rows = await service.list_participants(data["collection_id"])
    selected = set(data.get("selected", []))
    value = callback.data.split(":")[1]
    if value == "all":
        selected = {row["id"] for row in rows}
    elif value == "done":
        if not selected:
            await callback.answer("Выберите хотя бы одного участника", show_alert=True)
            return
        await state.update_data(selected=list(selected))
        await state.set_state(AddExpense.comment)
        await callback.answer("✅ Участники выбраны")
        await callback.message.answer(
            "Кратко опишите затрату (например, «билеты»). Отправьте <b>—</b>, если без комментария.",
            parse_mode="HTML",
        )
        return
    else:
        user_id = int(value)
        selected.symmetric_difference_update({user_id})
    await state.update_data(selected=list(selected))
    await callback.answer("✅ Выбор обновлен")
    await safe_edit(
        callback.message,
        "На кого разделить эту сумму? Можно отметить и самого плательщика.",
        participant_picker(rows, selected),
    )


@router.message(AddExpense.comment)
async def expense_comment(message: Message, state: FSMContext, service: BudgetService) -> None:
    data = await state.get_data()
    comment = "" if (message.text or "").strip() in ("-", "—") else (message.text or "").strip()
    transaction_id = await service.add_expense(
        data["collection_id"], message.from_user.id, data["amount"], data["selected"], comment
    )
    collection = await service.get_collection(data["collection_id"])
    await notify_subscribers(
        message.bot,
        service,
        collection,
        f"💸 {escape(message.from_user.full_name)} добавил затрату "
        f"<b>{format_money(data['amount'], collection['currency'])}</b>"
        f" · {escape(comment) if comment else 'без комментария'}.",
        exclude_user_ids={message.from_user.id},
    )
    await state.clear()
    await message.answer(
        f"✅ Затрата #{transaction_id} добавлена: "
        f"<b>{format_money(data['amount'], collection['currency'])}</b>.",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )
    await message.answer(
        await collection_text(service, collection),
        reply_markup=await collection_markup(
            message, collection, True, collection["admin_id"] == message.from_user.id
        ),
        parse_mode="HTML",
    )


@router.message(Command("repay"))
@router.message(F.text == "🤝 Вернуть долг")
async def repay_start(message: Message, state: FSMContext, service: BudgetService) -> None:
    await sync_user(service, message)
    await state.clear()
    await choose_collection(message, service, "repay", "Нет активных сборов для возврата долга.")


@router.callback_query(F.data.startswith("repay:"))
async def repay_collection(
    callback: CallbackQuery, state: FSMContext, service: BudgetService
) -> None:
    await sync_user(service, callback)
    collection_id = int(callback.data.split(":")[1])
    if not await service.is_participant(collection_id, callback.from_user.id):
        raise DomainError("Сначала вступите в сбор")
    snapshot = await service.collection_snapshot(collection_id)
    creditor_ids = {
        debt.creditor_id for debt in snapshot.debts if debt.debtor_id == callback.from_user.id
    }
    rows = [row for row in snapshot.participants if row["id"] in creditor_ids]
    if not rows:
        await callback.answer("По текущему балансу у вас нет долгов", show_alert=True)
        return
    await state.set_state(AddRepayment.creditor)
    await state.update_data(collection_id=collection_id)
    await callback.answer("⏳ Проверяю ваш баланс…")
    await callback.message.answer(
        "Кому вы вернули долг?",
        reply_markup=people_keyboard(rows, "creditor", callback.from_user.id),
    )


@router.callback_query(AddRepayment.creditor, F.data.startswith("creditor:"))
async def repay_creditor(
    callback: CallbackQuery, state: FSMContext, service: BudgetService
) -> None:
    creditor_id = int(callback.data.split(":")[1])
    await state.update_data(creditor_id=creditor_id)
    await state.set_state(AddRepayment.amount)
    await callback.answer("✅ Получатель выбран")
    creditor = await service.get_user(creditor_id)
    details = creditor["payment_details"].strip() if creditor else ""
    payment_text = (
        f"<b>💳 Данные для перевода · {escape(creditor['full_name'])}</b>\n{escape(details)}"
        if details
        else f"<b>💳 {escape(creditor['full_name'])}</b>\nПлатежные данные пока не добавлены."
    )
    await callback.message.answer(
        f"{payment_text}\n\nВведите фактически переведенную сумму:",
        parse_mode=ParseMode.HTML,
    )


@router.message(AddRepayment.amount)
async def repay_amount(message: Message, state: FSMContext, service: BudgetService) -> None:
    try:
        amount = parse_amount(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    transaction_id = await service.add_repayment(
        data["collection_id"], message.from_user.id, data["creditor_id"], amount
    )
    collection = await service.get_collection(data["collection_id"])
    await state.clear()
    await message.answer(
        f"⏳ Возврат #{transaction_id} на сумму "
        f"<b>{format_money(amount, collection['currency'])}</b> отправлен получателю на "
        "подтверждение. До подтверждения баланс не изменится.",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )
    confirm_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить получение",
                    callback_data=f"repayconfirm:{transaction_id}",
                )
            ]
        ]
    )
    await notify_subscribers(
        message.bot,
        service,
        collection,
        f"⏳ {escape(message.from_user.full_name)} сообщил о возврате "
        f"<b>{format_money(amount, collection['currency'])}</b>. "
        "Баланс изменится после подтверждения получателем.",
        exclude_user_ids={message.from_user.id, data["creditor_id"]},
    )
    try:
        await message.bot.send_message(
            data["creditor_id"],
            f"🤝 <b>Подтвердите получение</b>\n\nВозврат #{transaction_id}: "
            f"<b>{format_money(amount, collection['currency'])}</b>\n"
            f"Сбор: {escape(collection['title'])}",
            parse_mode=ParseMode.HTML,
            reply_markup=confirm_markup,
        )
    except TelegramAPIError:
        await message.answer(
            "ℹ️ Не удалось отправить личное уведомление получателю. Он сможет подтвердить "
            "возврат в истории сбора."
        )
    await message.answer(
        await collection_text(service, collection),
        reply_markup=await collection_markup(
            message, collection, True, collection["admin_id"] == message.from_user.id
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("repayconfirm:"))
async def repayment_confirm(callback: CallbackQuery, service: BudgetService) -> None:
    transaction_id = int(callback.data.split(":")[1])
    collection_id = await service.confirm_repayment(transaction_id, callback.from_user.id)
    await callback.answer("Получение подтверждено. Балансы пересчитаны.", show_alert=True)
    collection = await service.get_collection(collection_id)
    transaction = await service.transaction(transaction_id)
    await notify_subscribers(
        callback.bot,
        service,
        collection,
        f"✅ {escape(callback.from_user.full_name)} подтвердил получение возврата "
        f"#{transaction_id}: <b>{format_money(transaction['amount'], collection['currency'])}</b>.",
        exclude_user_ids={callback.from_user.id},
    )
    await safe_edit(
        callback.message,
        f"✅ <b>Получение подтверждено</b>\n\nВозврат #{transaction_id} учтён в балансах.",
    )
    await callback.message.answer(
        await collection_text(service, collection),
        reply_markup=await collection_markup(
            callback.message,
            collection,
            True,
            collection["admin_id"] == callback.from_user.id,
        ),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("history:"))
async def history(callback: CallbackQuery, service: BudgetService) -> None:
    await callback.answer("⏳ Загружаю историю…")
    await sync_user(service, callback)
    _, collection_id_raw, offset_raw = callback.data.split(":")
    collection_id, offset = int(collection_id_raw), int(offset_raw)
    if not await service.is_participant(collection_id, callback.from_user.id):
        raise DomainError("История доступна только участникам сбора")
    collection = await service.get_collection(collection_id)
    page = await service.history(collection_id, 11, offset)
    rows = page[:10]
    snapshot = await service.collection_snapshot(collection_id)
    participants = snapshot.participants
    active_count = sum(bool(row["active"]) for row in participants)
    text = history_text(collection, rows, snapshot.total, active_count)
    member_events = [
        row
        for row in await service.collection_events(collection_id)
        if row["kind"] in {"joined", "left", "member_removed"}
    ]
    if member_events:
        text += "\n\n<b>Изменения участников</b>\n"
        for event in member_events[:20]:
            if event["kind"] == "joined":
                action = "вступил в сбор"
            elif event["kind"] == "left":
                action = "вышел из сбора"
            else:
                action = f"удалил участника {escape(event['target_name'] or '')}"
            text += f"\n• {event['created_at'][:16]} · {escape(event['actor_name'])} {action}"
    debts = snapshot.debts
    names = {row["id"]: user_label(row) for row in participants}
    text += "\n\n<b>Финальный список балансов</b>\n"
    text += (
        "\n".join(
            f"• {names[d.debtor_id]} → {names[d.creditor_id]}: "
            f"<b>{format_money(d.amount, collection['currency'])}</b>"
            for d in debts
        )
        if debts
        else "✅ Все рассчитались"
    )
    await safe_edit(
        callback.message,
        text,
        history_keyboard(collection_id, rows, offset, len(page) > 10),
    )


@router.callback_query(F.data.startswith("tx:"))
async def transaction_details(callback: CallbackQuery, service: BudgetService) -> None:
    await sync_user(service, callback)
    transaction = await service.transaction(int(callback.data.split(":")[1]))
    if not transaction or not await service.is_participant(
        transaction["collection_id"], callback.from_user.id
    ):
        raise DomainError("Транзакция не найдена")
    collection = await service.get_collection(transaction["collection_id"])
    await callback.answer("⏳ Открываю транзакцию…")
    await safe_edit(
        callback.message,
        await transaction_text(service, transaction),
        transaction_actions(transaction, collection, callback.from_user.id),
    )


@router.callback_query(F.data.startswith("txcancel:"))
async def transaction_cancel_prompt(callback: CallbackQuery, service: BudgetService) -> None:
    transaction_id = int(callback.data.split(":")[1])
    transaction = await service.transaction(transaction_id)
    if not transaction:
        raise DomainError("Транзакция не найдена")
    await callback.answer("⚠️ Подтвердите отмену")
    await callback.message.answer(
        f"Отменить транзакцию #{transaction_id}? Она останется в истории.",
        reply_markup=confirmation("txcancel", transaction_id, f"tx:{transaction_id}"),
    )


@router.callback_query(F.data.startswith("txcancelyes:"))
async def transaction_cancel(callback: CallbackQuery, service: BudgetService) -> None:
    transaction_id = int(callback.data.split(":")[1])
    collection_id = await service.cancel_transaction(transaction_id, callback.from_user.id)
    collection = await service.get_collection(collection_id)
    await notify_subscribers(
        callback.bot,
        service,
        collection,
        f"↩️ {escape(callback.from_user.full_name)} отменил транзакцию #{transaction_id}. "
        "Балансы пересчитаны.",
        exclude_user_ids={callback.from_user.id},
    )
    await callback.answer("Транзакция отменена", show_alert=True)
    await show_collection(callback.message, collection_id, callback.from_user.id, service)


@router.callback_query(F.data.startswith("txedit:"))
async def transaction_edit_start(
    callback: CallbackQuery, state: FSMContext, service: BudgetService
) -> None:
    transaction = await service.transaction(int(callback.data.split(":")[1]))
    if not transaction:
        raise DomainError("Транзакция не найдена")
    collection = await service.get_collection(transaction["collection_id"])
    if callback.from_user.id not in (transaction["creator_id"], collection["admin_id"]):
        raise DomainError("Недостаточно прав")
    await state.set_state(EditTransaction.amount)
    await state.update_data(transaction_id=transaction["id"])
    await callback.answer("✏️ Переходим к редактированию")
    await callback.message.answer("Введите новую сумму:")


@router.message(EditTransaction.amount)
async def transaction_edit_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = parse_amount(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(amount=amount)
    await state.set_state(EditTransaction.comment)
    await message.answer("Введите новый комментарий или отправьте <b>—</b>:", parse_mode="HTML")


@router.message(EditTransaction.comment)
async def transaction_edit_comment(
    message: Message, state: FSMContext, service: BudgetService
) -> None:
    data = await state.get_data()
    comment = "" if (message.text or "").strip() in ("-", "—") else (message.text or "")
    collection_id = await service.edit_transaction(
        data["transaction_id"], message.from_user.id, data["amount"], comment
    )
    await state.clear()
    await message.answer("✅ Транзакция обновлена.", reply_markup=main_menu())
    collection = await service.get_collection(collection_id)
    await notify_subscribers(
        message.bot,
        service,
        collection,
        f"✏️ {escape(message.from_user.full_name)} обновил транзакцию "
        f"#{data['transaction_id']}: "
        f"<b>{format_money(data['amount'], collection['currency'])}</b>.",
        exclude_user_ids={message.from_user.id},
    )
    await message.answer(
        await collection_text(service, collection),
        reply_markup=await collection_markup(
            message, collection, True, collection["admin_id"] == message.from_user.id
        ),
        parse_mode="HTML",
    )


@router.message(Command("balance"))
@router.message(F.text == "⚖️ Мой баланс")
async def balance_start(message: Message, service: BudgetService) -> None:
    await sync_user(service, message)
    await choose_collection(message, service, "mybalance", "У вас пока нет активных сборов.")


@router.callback_query(F.data.startswith("mybalance:"))
async def my_balance(callback: CallbackQuery, service: BudgetService) -> None:
    await callback.answer("⏳ Считаю ваш баланс…")
    collection_id = int(callback.data.split(":")[1])
    collection = await service.get_collection(collection_id)
    if not await service.is_participant(collection_id, callback.from_user.id):
        raise DomainError("Вы не участвуете в этом сборе")
    snapshot = await service.collection_snapshot(collection_id)
    participants = snapshot.participants
    names = {row["id"]: user_label(row) for row in participants}
    debts = snapshot.debts
    personal = [
        debt for debt in debts if callback.from_user.id in (debt.debtor_id, debt.creditor_id)
    ]
    lines = [f"<b>⚖️ Ваш баланс · {escape(collection['title'])}</b>"]
    if not personal:
        lines.append("\n✅ У вас нет долгов и никто не должен вам.")
    for debt in personal:
        if debt.debtor_id == callback.from_user.id:
            lines.append(
                f"\nВы должны {names[debt.creditor_id]}: "
                f"<b>{format_money(debt.amount, collection['currency'])}</b>"
            )
        else:
            lines.append(
                f"\n{names[debt.debtor_id]} должен вам: "
                f"<b>{format_money(debt.amount, collection['currency'])}</b>"
            )
    await safe_edit(
        callback.message,
        "".join(lines),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="К сбору", callback_data=f"open:{collection_id}")]
            ]
        ),
    )


@router.callback_query(F.data.startswith("members:"))
async def members(callback: CallbackQuery, service: BudgetService) -> None:
    collection_id = int(callback.data.split(":")[1])
    if not await service.is_participant(collection_id, callback.from_user.id):
        raise DomainError("Список доступен участникам сбора")
    rows = await service.list_participants(collection_id)
    lines = ["<b>👥 Участники</b>"]
    for row in rows:
        payment = f" · 💳 {escape(row['payment_details'])}" if row["payment_details"] else ""
        crown = " 👑" if row["is_admin"] else ""
        lines.append(f"• {user_label(row)}{crown}{payment}")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Выйти из сбора", callback_data=f"leave:{collection_id}")],
            [InlineKeyboardButton(text="К сбору", callback_data=f"open:{collection_id}")],
        ]
    )
    await callback.answer("✅ Список участников готов")
    await safe_edit(callback.message, "\n".join(lines), keyboard)


@router.callback_query(F.data.startswith("leave:"))
async def leave(callback: CallbackQuery, service: BudgetService) -> None:
    collection_id = int(callback.data.split(":")[1])
    collection = await service.get_collection(collection_id)
    await service.remove_participant(collection_id, callback.from_user.id, callback.from_user.id)
    await notify_subscribers(
        callback.bot,
        service,
        collection,
        f"👋 {escape(callback.from_user.full_name)} вышел из сбора.",
        exclude_user_ids={callback.from_user.id},
    )
    await callback.answer("Вы вышли из сбора", show_alert=True)
    await safe_edit(
        callback.message,
        f"Вы больше не участвуете в сборе <b>{escape(collection['title'])}</b>.",
    )


@router.message(F.text == "💳 Платежные данные")
async def payment_start(message: Message, state: FSMContext, service: BudgetService) -> None:
    await sync_user(service, message)
    await state.set_state(PaymentDetails.details)
    await message.answer(
        "Отправьте платежные данные одним сообщением (например, номер телефона СБП или последние "
        "цифры карты). Они будут видны участникам ваших сборов. Для удаления отправьте <b>—</b>.",
        parse_mode="HTML",
    )


@router.message(PaymentDetails.details)
async def payment_save(message: Message, state: FSMContext, service: BudgetService) -> None:
    details = "" if (message.text or "").strip() in ("-", "—") else (message.text or "")
    await service.set_payment_details(message.from_user.id, details)
    await state.clear()
    await message.answer("✅ Платежные данные сохранены.", reply_markup=main_menu())


@router.callback_query(F.data.startswith("manage:"))
async def manage(callback: CallbackQuery, service: BudgetService) -> None:
    collection_id = int(callback.data.split(":")[1])
    collection = await service.get_collection(collection_id)
    if collection["admin_id"] != callback.from_user.id:
        raise DomainError("Недостаточно прав")
    await callback.answer("⚙️ Открываю управление…")
    await safe_edit(
        callback.message,
        f"<b>⚙️ Управление · {escape(collection['title'])}</b>",
        admin_actions(collection_id),
    )


@router.callback_query(F.data.startswith("archive:"))
async def archive_prompt(callback: CallbackQuery) -> None:
    collection_id = int(callback.data.split(":")[1])
    await callback.answer("📦 Подтвердите завершение сбора")
    await callback.message.answer(
        "Завершить сбор? Он попадет в архив и будет доступен для восстановления 30 дней.",
        reply_markup=confirmation("archive", collection_id),
    )


@router.callback_query(F.data.startswith("archiveyes:"))
async def archive_confirm(callback: CallbackQuery, service: BudgetService) -> None:
    collection_id = int(callback.data.split(":")[1])
    collection = await service.get_collection(collection_id)
    await service.archive(collection_id, callback.from_user.id)
    await notify_subscribers(
        callback.bot,
        service,
        collection,
        f"📦 {escape(callback.from_user.full_name)} завершил сбор. Архив — 30 дней.",
        exclude_user_ids={callback.from_user.id},
    )
    await callback.answer("Сбор перемещен в архив", show_alert=True)
    await show_collection(callback.message, collection_id, callback.from_user.id, service)


@router.callback_query(F.data.startswith("restore:"))
async def restore(callback: CallbackQuery, service: BudgetService) -> None:
    collection_id = int(callback.data.split(":")[1])
    await service.restore(collection_id, callback.from_user.id)
    collection = await service.get_collection(collection_id)
    await notify_subscribers(
        callback.bot,
        service,
        collection,
        f"♻️ {escape(callback.from_user.full_name)} восстановил сбор.",
        exclude_user_ids={callback.from_user.id},
    )
    await callback.answer("Сбор восстановлен", show_alert=True)
    await show_collection(callback.message, collection_id, callback.from_user.id, service)


@router.callback_query(F.data.startswith("transfer:"))
async def transfer_prompt(callback: CallbackQuery, service: BudgetService) -> None:
    collection_id = int(callback.data.split(":")[1])
    collection = await service.get_collection(collection_id)
    if collection["admin_id"] != callback.from_user.id:
        raise DomainError("Недостаточно прав")
    rows = await service.list_participants(collection_id)
    await callback.answer("👑 Выберите нового администратора")
    await callback.message.answer(
        "Кому передать роль администратора?",
        reply_markup=people_keyboard(rows, f"transferdo:{collection_id}", callback.from_user.id),
    )


@router.callback_query(F.data.startswith("transferdo:"))
async def transfer_do(callback: CallbackQuery, service: BudgetService) -> None:
    _, collection_id, user_id = callback.data.split(":")
    await service.transfer_admin(int(collection_id), callback.from_user.id, int(user_id))
    collection = await service.get_collection(int(collection_id))
    new_admin = await service.get_user(int(user_id))
    await notify_subscribers(
        callback.bot,
        service,
        collection,
        f"👑 Новый администратор сбора — {escape(new_admin['full_name'])}.",
        exclude_user_ids={callback.from_user.id},
    )
    await callback.answer("Роль передана", show_alert=True)
    await show_collection(callback.message, int(collection_id), callback.from_user.id, service)


@router.callback_query(F.data.startswith("remove:"))
async def remove_prompt(callback: CallbackQuery, service: BudgetService) -> None:
    collection_id = int(callback.data.split(":")[1])
    collection = await service.get_collection(collection_id)
    if collection["admin_id"] != callback.from_user.id:
        raise DomainError("Недостаточно прав")
    rows = await service.list_participants(collection_id)
    await callback.answer("👥 Выберите участника")
    await callback.message.answer(
        "Кого удалить? Удалить можно только участника с нулевым балансом, которого еще нет в истории.",
        reply_markup=people_keyboard(rows, f"removedo:{collection_id}", callback.from_user.id),
    )


@router.callback_query(F.data.startswith("removedo:"))
async def remove_do(callback: CallbackQuery, service: BudgetService) -> None:
    _, collection_id, user_id = callback.data.split(":")
    collection = await service.get_collection(int(collection_id))
    member = await service.get_user(int(user_id))
    await service.remove_participant(int(collection_id), callback.from_user.id, int(user_id))
    await notify_subscribers(
        callback.bot,
        service,
        collection,
        f"👥 {escape(member['full_name'])} больше не участвует в сборе.",
        exclude_user_ids={callback.from_user.id, int(user_id)},
    )
    await callback.answer("Участник удален", show_alert=True)
    await show_collection(callback.message, int(collection_id), callback.from_user.id, service)


async def quick_expense(
    message: Message,
    payload: str,
    service: BudgetService,
    state: FSMContext,
    collection_id: int | None = None,
    actor_id: int | None = None,
) -> None:
    """Parse command arguments: 40 @ivan @maxim comment."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Быстрая запись доступна только внутри общей Telegram-группы.")
        return
    tokens = payload.split()
    try:
        amount = parse_amount(tokens[0])
    except ValueError as exc:
        await message.reply(str(exc))
        return
    actor_id = actor_id or message.from_user.id
    collections = await service.list_collections(actor_id, message.chat.id, include_archived=False)
    if not collections:
        await message.reply("Сначала нажмите «Участвовать в сборе» под сообщением нужного сбора.")
        return
    if collection_id is None and len(collections) > 1:
        await state.set_state(AddExpense.collection)
        await state.update_data(quick_payload=payload)
        await message.reply(
            "В какой сбор добавить затрату?",
            reply_markup=collections_keyboard(collections, "quickexpense"),
        )
        return
    collection = next((row for row in collections if row["id"] == collection_id), collections[0])
    usernames = list(
        dict.fromkeys(
            token[1:].lower() for token in tokens[1:] if re.fullmatch(r"@[A-Za-z0-9_]+", token)
        )
    )
    comment_tokens = [token for token in tokens[1:] if not re.fullmatch(r"@[A-Za-z0-9_]+", token)]
    if not usernames:
        await message.reply(
            "Отметьте хотя бы одного участника после суммы или используйте кнопочный сценарий."
        )
        return
    participant_ids: list[int] = []
    unknown: list[str] = []
    for username in usernames:
        user = await service.user_by_username(username)
        if not user or not await service.is_participant(collection["id"], user["id"]):
            unknown.append(f"@{username}")
        else:
            participant_ids.append(user["id"])
    if unknown:
        await message.reply(
            "Не нажали «Участвовать в сборе» или сменили username: "
            + ", ".join(escape(item) for item in unknown)
        )
        return
    transaction_id = await service.add_expense(
        collection["id"],
        actor_id,
        amount,
        participant_ids,
        " ".join(comment_tokens),
    )
    await notify_subscribers(
        message.bot,
        service,
        collection,
        f"💸 {escape(message.from_user.full_name)} добавил затрату "
        f"<b>{format_money(amount, collection['currency'])}</b> · "
        f"{' '.join(escape(token) for token in comment_tokens) or 'без комментария'}.",
        exclude_user_ids={actor_id},
    )
    await message.reply(
        f"✅ Затрата #{transaction_id} добавлена в «{escape(collection['title'])}»: "
        f"<b>{format_money(amount, collection['currency'])}</b> на "
        f"{', '.join('@' + escape(name) for name in usernames)}.",
        parse_mode="HTML",
    )
    await state.clear()


@router.callback_query(AddExpense.collection, F.data.startswith("quickexpense:"))
async def quick_expense_collection(
    callback: CallbackQuery, state: FSMContext, service: BudgetService
) -> None:
    await sync_user(service, callback)
    data = await state.get_data()
    await callback.answer("⚡ Добавляю затрату…")
    await quick_expense(
        callback.message,
        data["quick_payload"],
        service,
        state,
        int(callback.data.split(":")[1]),
        callback.from_user.id,
    )


@router.inline_query()
async def inline_hint(inline_query: InlineQuery) -> None:
    bot_user = await inline_query.bot.get_me()
    app_url = (
        f"https://t.me/{bot_user.username}?startapp=home&mode=compact"
        if bot_user.has_main_web_app
        else f"https://t.me/{bot_user.username}?start=app"
    )
    await inline_query.answer(
        results=[
            InlineQueryResultArticle(
                id="shakeonit_help_v1",
                title="💡 Подсказка ShakeOnIt",
                description="Как начать, вступить в сбор и добавить затрату",
                input_message_content=InputTextMessageContent(
                    message_text=MENTION_HINT,
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📱 Открыть ShakeOnIt", url=app_url)]
                    ]
                ),
            )
        ],
        cache_time=1,
        is_personal=True,
    )


@router.message(StateFilter(None), F.text.regexp(r"(?i)@ShakeOnIt_bot"))
async def mention_hint(message: Message) -> None:
    await message.reply(MENTION_HINT, parse_mode=ParseMode.HTML)


@router.message(StateFilter(None))
async def unknown_action(message: Message) -> None:
    await message.answer(
        "ℹ️ Не удалось распознать действие. Выберите нужный пункт в меню — так быстрее и надежнее.",
        reply_markup=main_menu(),
    )


@router.error()
async def domain_error_handler(event) -> bool:
    exception = event.exception
    if not isinstance(exception, DomainError):
        return False
    update = event.update
    if update.callback_query:
        await update.callback_query.answer(str(exception), show_alert=True)
    elif update.message:
        await update.message.answer(f"⚠️ {escape(str(exception))}", parse_mode="HTML")
    return True
