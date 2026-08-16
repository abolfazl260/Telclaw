import asyncio

from ui import ConsoleUI


async def main_async():
    ui = ConsoleUI()
    await ui.run()


if __name__ == "__main__":
    asyncio.run(main_async())
