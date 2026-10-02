"""Application service for Telegram account/client lifecycle."""

import sessions_manager


class AccountService:
    async def list_accounts(self):
        return await sessions_manager.get_active_accounts()

    def create_client(self, account_name):
        return sessions_manager.create_client(account_name)

    async def connect(self, account_name):
        client = self.create_client(account_name)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise PermissionError(f"Telegram session '{account_name}' is not authorized")
        return client

    async def register(self, session_name):
        return await sessions_manager.register_new_account(session_name)

    async def begin_registration(self, session_name, phone):
        return await sessions_manager.begin_account_registration(session_name, phone)

    async def submit_registration_code(self, session_name, code):
        return await sessions_manager.submit_account_registration_code(session_name, code)

    async def submit_registration_password(self, session_name, password):
        return await sessions_manager.submit_account_registration_password(session_name, password)

    async def cancel_registration(self, session_name):
        return await sessions_manager.cancel_account_registration(session_name)

    def registration_state(self, session_name=None):
        return sessions_manager.get_account_registration_state(session_name)

    async def disconnect(self, client):
        if client is not None:
            await client.disconnect()
