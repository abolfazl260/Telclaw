"""Console presentation layer for Telclaw.

The UI is intentionally thin: it collects user input and delegates business
operations to application services. Console input runs in a worker thread so
background crawler tasks keep running while the menu waits for input.
"""

import asyncio
import os

from colorama import Fore, Style, init

import config
from services.account_service import AccountService
from services.channel_service import ChannelService
from services.crawler_service import CrawlerService

init(autoreset=True)


class ConsoleUI:
    def __init__(self, account_service=None, channel_service=None, crawler_service=None):
        self.accounts = account_service or AccountService()
        self.channels = channel_service or ChannelService()
        self.crawler = crawler_service or CrawlerService(self.channels)
        self.client = None

    def clear_screen(self):
        os.system("cls" if os.name == "nt" else "clear")

    def show_banner(self):
        print(
            f"{Fore.CYAN}╔════════════════════════════════════════════════════════════╗\n"
            f"║{Fore.MAGENTA}       🤖 TELEGRAM ADVANCED CRAWLER TUI 🤖{Fore.CYAN}              ║\n"
            f"║{Fore.BLUE}        Advanced Telegram Message Crawler Tool{Fore.CYAN}            ║\n"
            f"╚════════════════════════════════════════════════════════════╝{Style.RESET_ALL}"
        )

    def show_section_header(self, title):
        print(f"\n{Fore.CYAN}┌─ {Fore.WHITE}{title}{Fore.CYAN} ─┐{Style.RESET_ALL}")

    def show_section_footer(self):
        print(f"{Fore.CYAN}└{'─' * 56}┘{Style.RESET_ALL}")

    def show_message(self, message, color=Fore.CYAN):
        print(f"{color}▸ {message}{Style.RESET_ALL}")

    async def pause(self, message="Press Enter to continue..."):
        await asyncio.to_thread(input, f"\n{message}")

    async def prompt_choice(self, prompt, valid_options):
        while True:
            value = (await asyncio.to_thread(input, prompt)).strip().lower()
            if value in valid_options:
                return value
            self.show_message("Invalid choice. Please try again.", Fore.RED)

    async def prompt_text(self, prompt, default=None, allow_empty=True):
        while True:
            suffix = f" [{default}]" if default is not None else ""
            value = (await asyncio.to_thread(input, f"{prompt}{suffix}: ")).strip()
            value = value or (default if default is not None else "")
            if allow_empty or value:
                return value
            self.show_message("This field cannot be empty.", Fore.RED)

    def _print_options(self, title, items):
        self.show_section_header(title)
        for index, item in enumerate(items, start=1):
            print(f"{Fore.GREEN}│  {Fore.YELLOW}[{index}]{Fore.WHITE} {item}")
        self.show_section_footer()

    async def connect_client(self, account_name=None):
        if self.client is not None:
            return self.client

        accounts = self.accounts.list_accounts()
        if not accounts:
            self.show_message("No active Telegram sessions were found.", Fore.YELLOW)
            return None

        if account_name is None:
            labels = [account["session"] for account in accounts]
            self._print_options("Select Account", labels)
            choice = await self.prompt_text("Select account number", default="1")
            try:
                account_name = accounts[int(choice) - 1]["session"]
            except (ValueError, IndexError):
                self.show_message("Invalid account selection.", Fore.RED)
                return None

        try:
            self.show_message(f"Connecting to Telegram for '{account_name}'...")
            self.client = await self.accounts.connect(account_name)
            self.show_message(f"Connected to '{account_name}'.", Fore.GREEN)
            return self.client
        except Exception as exc:
            self.show_message(f"Unable to connect: {exc}", Fore.RED)
            return None

    async def account_menu(self):
        while True:
            self.clear_screen()
            self.show_banner()
            self.show_section_header("Account Management")
            print(f"{Fore.GREEN}│  1. Select an existing account")
            print(f"{Fore.GREEN}│  2. Add a new account")
            print(f"{Fore.GREEN}│  3. Go back")
            self.show_section_footer()

            choice = await self.prompt_choice("\nChoose an option [1-3]: ", {"1", "2", "3"})
            if choice == "3":
                return

            if choice == "1":
                await self.connect_client()
                await self.pause()
                return

            existing_names = {account["session"] for account in self.accounts.list_accounts()}
            while True:
                name = await self.prompt_text("Enter a unique session name", allow_empty=False)
                if name not in existing_names:
                    break
                self.show_message(
                    f"Session '{name}' already exists. Choose another name or select it from option 1.",
                    Fore.YELLOW,
                )

            self.show_message(
                "A new Telegram login will now be started. You will be asked for your phone number, "
                "verification code, and 2FA password if enabled.",
                Fore.CYAN,
            )
            if await self.accounts.register(name):
                self.show_message(f"Account '{name}' created successfully.", Fore.GREEN)
            else:
                self.show_message("Account creation failed. No account was added.", Fore.RED)
            await self.pause()

    async def start_crawler_flow(self):
        self.clear_screen()
        self.show_banner()
        self.show_section_header("Scheduled Crawler")

        try:
            categories = self.crawler.categories()
        except Exception as exc:
            self.show_message(f"Unable to load channels: {exc}", Fore.RED)
            await self.pause()
            return

        if not categories:
            self.show_message("No categories found.", Fore.YELLOW)
            await self.pause()
            return

        self._print_options("Available Categories", categories)
        choice = await self.prompt_text("Select category number", default="1")
        try:
            category = categories[int(choice) - 1]
        except (ValueError, IndexError):
            self.show_message("Invalid category selection.", Fore.RED)
            await self.pause()
            return

        interval = await self.prompt_text(
            "Crawl interval in hours",
            default=str(getattr(config, "CRAWL_INTERVAL_HOURS", 5)),
            allow_empty=False,
        )
        try:
            interval_hours = float(interval)
            if interval_hours <= 0:
                raise ValueError
        except ValueError:
            self.show_message("Interval must be a positive number.", Fore.RED)
            await self.pause()
            return

        client = await self.connect_client()
        if client is None:
            await self.pause()
            return

        try:
            jobs = self.crawler.schedule_category(client, category, interval_hours)
        except Exception as exc:
            self.show_message(f"Unable to schedule crawler: {exc}", Fore.RED)
            await self.pause()
            return

        self.show_message(
            f"Category '{category}' scheduled: {len(jobs)} channel(s), "
            f"every {interval_hours:g} hour(s). First crawl starts immediately.",
            Fore.GREEN,
        )
        await self.pause("Press Enter to return to the menu. Jobs continue in background...")

    async def change_settings(self):
        self.clear_screen()
        self.show_banner()
        self.show_section_header("Settings")
        print(f"{Fore.GREEN}│  Base Delay: {config.BASE_DELAY} seconds")
        print(f"{Fore.GREEN}│  Random Delay Max: {config.RANDOM_DELAY_MAX} seconds")
        print(f"{Fore.GREEN}│  Crawl Interval: {getattr(config, 'CRAWL_INTERVAL_HOURS', 5)} hours")
        self.show_section_footer()

        try:
            config.BASE_DELAY = int(await self.prompt_text("New base delay", str(config.BASE_DELAY)))
            config.RANDOM_DELAY_MAX = int(
                await self.prompt_text("New random delay max", str(config.RANDOM_DELAY_MAX))
            )
            config.CRAWL_INTERVAL_HOURS = float(
                await self.prompt_text(
                    "New crawl interval in hours",
                    str(getattr(config, "CRAWL_INTERVAL_HOURS", 5)),
                )
            )
            if config.CRAWL_INTERVAL_HOURS <= 0:
                raise ValueError
            self.show_message("Settings updated successfully.", Fore.GREEN)
        except ValueError:
            self.show_message("Settings contain invalid values.", Fore.RED)
        await self.pause()

    async def manage_channels(self):
        self.clear_screen()
        self.show_banner()
        try:
            data = self.channels.load()
        except Exception as exc:
            self.show_message(f"Unable to read channels: {exc}", Fore.RED)
            await self.pause()
            return

        self.show_section_header("Channel Management")
        for category, channels in data.items():
            print(f"{Fore.CYAN}│  [{category}]")
            for channel in channels:
                print(f"{Fore.GREEN}│    ├─ @{channel.get('username', 'unknown')}")
                print(f"{Fore.GREEN}│    └─ {Fore.WHITE}{channel.get('description', 'No description')}")
        self.show_section_footer()
        await self.pause()

    async def run(self):
        while True:
            self.clear_screen()
            self.show_banner()
            self.show_section_header("Main Menu")
            print(f"{Fore.GREEN}│  1. ▶️ Start scheduled crawler")
            print(f"{Fore.GREEN}│  2. ⚙️ Change settings")
            print(f"{Fore.GREEN}│  3. 📋 Manage channels")
            print(f"{Fore.GREEN}│  4. 👤 Switch / add account")
            print(f"{Fore.GREEN}│  5. 🚪 Exit")
            self.show_section_footer()

            choice = await self.prompt_choice("\nChoose an option [1-5]: ", {"1", "2", "3", "4", "5"})
            if choice == "1":
                await self.start_crawler_flow()
            elif choice == "2":
                await self.change_settings()
            elif choice == "3":
                await self.manage_channels()
            elif choice == "4":
                await self.account_menu()
            else:
                self.crawler.stop_all()
                await self.accounts.disconnect(self.client)
                self.client = None
                break


async def account_menu():
    return await ConsoleUI().account_menu()


async def run_ui():
    await ConsoleUI().run()
