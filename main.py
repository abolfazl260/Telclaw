import asyncio

from storage import database
from ui import ConsoleUI


def main():
    database.initialize_db()
    asyncio.run(ConsoleUI().run())


if __name__ == "__main__":
    main()
