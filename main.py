import asyncio

from storage import database
from system_ui import SystemConsoleUI


def main():
    database.initialize_db()
    asyncio.run(SystemConsoleUI().run())


if __name__ == "__main__":
    main()
