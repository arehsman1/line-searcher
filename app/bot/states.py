"""FSM states for multi-step flows."""
from aiogram.fsm.state import State, StatesGroup


class SearchStates(StatesGroup):
    waiting_keywords = State()
    confirm = State()


class AdminStorageStates(StatesGroup):
    waiting_chat_id = State()
    waiting_name = State()
    waiting_forward = State()


class BroadcastStates(StatesGroup):
    waiting_message = State()
    confirming = State()


class AdminUserStates(StatesGroup):
    waiting_user_query = State()
    waiting_allow_plan = State()
