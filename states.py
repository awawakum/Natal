from aiogram.fsm.state import State, StatesGroup


class TarotFlow(StatesGroup):
    choosing_topic = State()
    awaiting_question = State()


class NatalFlow(StatesGroup):
    awaiting_birth_date = State()
    awaiting_chart_confirmation = State()


class FollowUpFlow(StatesGroup):
    awaiting_reply = State()


class ImageFlow(StatesGroup):
    awaiting_image = State()
