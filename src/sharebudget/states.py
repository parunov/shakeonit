from aiogram.fsm.state import State, StatesGroup


class AddExpense(StatesGroup):
    collection = State()
    amount = State()
    participants = State()
    comment = State()


class AddRepayment(StatesGroup):
    collection = State()
    creditor = State()
    amount = State()


class PaymentDetails(StatesGroup):
    details = State()


class EditTransaction(StatesGroup):
    amount = State()
    comment = State()
