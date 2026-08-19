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

    async def disconnect(self, client):
        if client is not None:
            await client.disconnect()
