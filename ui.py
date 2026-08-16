
import json

import os

from datetime import datetime



from colorama import Fore, Style, init



import config

import scheduler

import sessions_manager



init(autoreset=True)





class ConsoleUI:

    def __init__(self):

        self.client = None



    def clear_screen(self):

        os.system("cls" if os.name == "nt" else "clear")



    def show_banner(self):

        banner = f"""

{Fore.CYAN}╔════════════════════════════════════════════════════════════╗

║{Fore.MAGENTA}       🤖 TELEGRAM ADVANCED CRAWLER TUI 🤖{Fore.CYAN}              ║

║{Fore.BLUE}        Advanced Telegram Message Crawler Tool{Fore.CYAN}            ║

╚════════════════════════════════════════════════════════════╝{Style.RESET_ALL}

        """

        print(banner)



    def show_section_header(self, title):

        """Display a section header with visual styling"""

        print(f"\n{Fore.CYAN}┌─ {Fore.WHITE}{title}{Fore.CYAN} ─┐{Style.RESET_ALL}")



    def show_section_footer(self):

        """Display a section footer"""

        print(f"{Fore.CYAN}└{'─' * 56}┘{Style.RESET_ALL}")



    def show_message(self, message, color=Fore.CYAN):

        print(f"{color}▸ {message}{Style.RESET_ALL}")



    def pause(self, message="Press Enter to continue..."):

        input(f"\n{message}")



    def prompt_choice(self, prompt, valid_options, allow_back=False):

        while True:

            value = input(prompt).strip().lower()

            if allow_back and value == "b":

                return "b"

            if value in valid_options:

                return value

            self.show_message("Invalid choice. Please try again.", Fore.RED)



    def prompt_text(self, prompt, default=None, allow_empty=True):

        while True:

            if default is None:

                value = input(f"{prompt}: ").strip()

            else:

                value = input(f"{prompt} [{default}]: ").strip() or default

            if allow_empty or value:

                return value

            self.show_message("This field cannot be empty.", Fore.RED)



    async def connect_client(self, account_name=None):

        if self.client is not None:

            return self.client



        accounts = sessions_manager.get_active_accounts()

        if not accounts:

            self.show_message("No active Telegram sessions were found. Create one first.", Fore.YELLOW)

            return None



        if account_name is None:

            self.show_section_header("Select Account")

            print(f"\n{Fore.WHITE}│")

            for index, account in enumerate(accounts, start=1):

                print(f"{Fore.GREEN}│  {Fore.YELLOW}[{index}]{Fore.WHITE} {account}")

            print(f"{Fore.WHITE}│{Style.RESET_ALL}")

            self.show_section_footer()



            choice = self.prompt_text("Select account number", default="1")

            try:

                account_name = accounts[int(choice) - 1]

            except (ValueError, IndexError):

                self.show_message("Invalid account selection.", Fore.RED)

                return None



        self.show_message(f"Connecting to Telegram for '{account_name}'...", Fore.CYAN)

        client = sessions_manager.create_client(account_name)

        await client.connect()



        if not await client.is_user_authorized():

            await client.disconnect()

            self.show_message("The selected session is not authorized. Please log in again.", Fore.RED)

            return None



        self.client = client

        return client



    async def account_menu(self):

        while True:

            self.clear_screen()

            self.show_banner()

            self.show_section_header("Account Management")

            print(f"{Fore.WHITE}│")

            print(f"{Fore.GREEN}│  {Fore.CYAN}1{Fore.WHITE}. Select an existing account")

            print(f"{Fore.GREEN}│  {Fore.CYAN}2{Fore.WHITE}. Add a new account")

            print(f"{Fore.GREEN}│  {Fore.CYAN}3{Fore.WHITE}. Go back")

            print(f"{Fore.WHITE}│{Style.RESET_ALL}")

            self.show_section_footer()



            choice = self.prompt_choice(f"\n{Fore.YELLOW}Choose an option [1-3]: {Style.RESET_ALL}", {"1", "2", "3"})



            if choice == "3":

                return None



            if choice == "1":

                accounts = sessions_manager.get_active_accounts()

                if not accounts:

                    self.show_message("No accounts found. You can create one now.", Fore.YELLOW)

                    self.pause()

                    continue



                self.show_section_header("Available Accounts")

                print(f"{Fore.WHITE}│")

                for index, account in enumerate(accounts, start=1):

                    print(f"{Fore.CYAN}│  {Fore.YELLOW}[{index}]{Fore.WHITE} {account}")

                print(f"{Fore.WHITE}│{Style.RESET_ALL}")

                self.show_section_footer()



                selected = self.prompt_text("Select account number", default="1")

                try:

                    account_name = accounts[int(selected) - 1]["session"]

                except (ValueError, IndexError):

                    self.show_message("Invalid account selection.", Fore.RED)

                    self.pause()

                    continue



                client = await self.connect_client(account_name)

                if client is None:

                    self.pause()

                    continue



                self.show_message(f"Connected to account '{account_name}'.", Fore.GREEN)

                self.pause()

                return account_name



            if choice == "2":

                name = self.prompt_text("Enter a unique session name", allow_empty=False)

                if await sessions_manager.register_new_account(name):

                    self.show_message(f"Account '{name}' created successfully.", Fore.GREEN)

                    self.pause()

                    return name



                self.show_message("Account creation failed.", Fore.RED)

                self.pause()



    async def start_crawler_flow(self):

        self.clear_screen()

        self.show_banner()

        self.show_section_header("Start Crawler")



        base_dir = os.path.dirname(os.path.abspath(__file__))

        channels_file = os.path.join(base_dir, "channels.json")



        if not os.path.exists(channels_file):

            self.show_message(f"Channel file not found: {channels_file}", Fore.RED)

            self.pause()

            return



        try:

            with open(channels_file, "r", encoding="utf-8") as handle:

                channels_data = json.load(handle)

        except Exception as exc:

            self.show_message(f"Unable to read channels.json: {exc}", Fore.RED)

            self.pause()

            return



        categories = list(channels_data.keys())

        if not categories:

            self.show_message("No categories found in channels.json.", Fore.YELLOW)

            self.pause()

            return



        print(f"\n{Fore.WHITE}│")

        print(f"{Fore.CYAN}│  {Fore.WHITE}Available Categories:")

        print(f"{Fore.WHITE}│")

        for index, category in enumerate(categories, start=1):

            print(f"{Fore.GREEN}│  {Fore.YELLOW}[{index}]{Fore.WHITE} {category}")

        print(f"{Fore.WHITE}│{Style.RESET_ALL}")

        self.show_section_footer()



        category_choice = self.prompt_text("Select category number", default="1")

        try:

            selected_category = categories[int(category_choice) - 1]

        except (ValueError, IndexError):

            self.show_message("Invalid category selection.", Fore.RED)

            self.pause()

            return



        channels_to_crawl = channels_data[selected_category]

        today_str = datetime.now().strftime("%Y-%m-%d")



        from_date = self.prompt_text("From date", default=today_str)

        to_date = self.prompt_text("To date", default=today_str)



        try:

            target_date = datetime.strptime(to_date, "%Y-%m-%d").date()

        except ValueError:

            self.show_message("Invalid date format. Use YYYY-MM-DD.", Fore.RED)

            self.pause()

            return



        client = await self.connect_client()

        if client is None:

            self.pause()

            return



        self.show_message(f"Starting crawler for category '{selected_category}'...", Fore.GREEN)

        for channel in channels_to_crawl:

            await scheduler.start_crawler(client, channel["username"], target_date)



        self.show_message("Crawler finished successfully.", Fore.GREEN)

        self.pause()



    async def change_settings(self):

        self.clear_screen()

        self.show_banner()

        self.show_section_header("Settings")

        

        print(f"\n{Fore.WHITE}│")

        print(f"{Fore.CYAN}│  {Fore.YELLOW}Current Configuration:")

        print(f"{Fore.WHITE}│")

        print(f"{Fore.GREEN}│  ✓ Base Delay: {Fore.YELLOW}{config.BASE_DELAY}{Fore.WHITE} seconds")

        print(f"{Fore.GREEN}│  ✓ Random Delay Max: {Fore.YELLOW}{config.RANDOM_DELAY_MAX}{Fore.WHITE} seconds")

        print(f"{Fore.WHITE}│{Style.RESET_ALL}")

        self.show_section_footer()



        new_base_delay = self.prompt_text("\nNew base delay", default=str(config.BASE_DELAY))

        new_random_delay = self.prompt_text("New random delay max", default=str(config.RANDOM_DELAY_MAX))



        try:

            config.BASE_DELAY = int(new_base_delay)

            config.RANDOM_DELAY_MAX = int(new_random_delay)

        except ValueError:

            self.show_message("Delay values must be integers.", Fore.RED)

            self.pause()

            return



        self.show_message("Settings updated successfully.", Fore.GREEN)

        self.pause()



    async def manage_channels(self):

        self.clear_screen()

        self.show_banner()

        self.show_section_header("Channel Management")



        base_dir = os.path.dirname(os.path.abspath(__file__))

        channels_file = os.path.join(base_dir, "channels.json")



        if not os.path.exists(channels_file):

            self.show_message(f"Channel file not found: {channels_file}", Fore.RED)

            self.pause()

            return



        try:

            with open(channels_file, "r", encoding="utf-8") as handle:

                data = json.load(handle)

        except Exception as exc:

            self.show_message(f"Unable to read channels.json: {exc}", Fore.RED)

            self.pause()

            return



        print(f"{Fore.WHITE}│")

        for category, channels in data.items():

            print(f"{Fore.CYAN}│  {Fore.MAGENTA}[{category}]{Style.RESET_ALL}")

            for channel in channels:

                username = channel.get('username', 'unknown')

                description = channel.get('description', 'No description')

                print(f"{Fore.GREEN}│    ├─ {Fore.YELLOW}@{username}")

                print(f"{Fore.GREEN}│    └─ {Fore.WHITE}{description}{Style.RESET_ALL}")

            print(f"{Fore.WHITE}│")

        print(f"{Fore.WHITE}│{Style.RESET_ALL}")

        self.show_section_footer()



        self.pause()



    async def run(self):

        while True:

            self.clear_screen()

            self.show_banner()

            self.show_section_header("Main Menu")

            

            print(f"{Fore.WHITE}│")

            print(f"{Fore.GREEN}│  {Fore.CYAN}1{Fore.WHITE}. ▶️ Start scheduled crawler")

            print(f"{Fore.GREEN}│  {Fore.CYAN}2{Fore.WHITE}. ⚙️ Change base delay and settings")

            print(f"{Fore.GREEN}│  {Fore.CYAN}3{Fore.WHITE}. 📋 Manage channels")

            print(f"{Fore.GREEN}│  {Fore.CYAN}4{Fore.WHITE}. 👤 Switch / add account")

            print(f"{Fore.GREEN}│  {Fore.CYAN}5{Fore.WHITE}. 🚪 Exit")

            print(f"{Fore.WHITE}│{Style.RESET_ALL}")

            self.show_section_footer()



            choice = self.prompt_choice(f"\n{Fore.YELLOW}Choose an option [1-5]: {Style.RESET_ALL}", {"1", "2", "3", "4", "5"})



            if choice == "1":

                await self.start_crawler_flow()

            elif choice == "2":

                await self.change_settings()

            elif choice == "3":

                await self.manage_channels()

            elif choice == "4":

                await self.account_menu()

            else:

                if self.client is not None:

                    try:

                        await self.client.disconnect()

                    except Exception:

                        pass

                self.clear_screen()

                print(f"{Fore.CYAN}╔════════════════════════════════════════════════════════════╗{Style.RESET_ALL}")

                print(f"{Fore.CYAN}║{Fore.YELLOW}          Thank you for using Telegram Crawler!{Fore.CYAN}           ║{Style.RESET_ALL}")

                print(f"{Fore.CYAN}║{Fore.YELLOW}                    Goodbye! 👋{Fore.CYAN}                          ║{Style.RESET_ALL}")

                print(f"{Fore.CYAN}╚════════════════════════════════════════════════════════════╝{Style.RESET_ALL}\n")

                break





async def account_menu():

    ui = ConsoleUI()

    return await ui.account_menu()





async def run_ui():

    ui = ConsoleUI()

    await ui.run()