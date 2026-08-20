"""System menu extensions for independently running database-backed queues."""

from colorama import Fore

from ai.ai_service import AIProcessingService
from ai.groq_connection_test import test_groq_connection
from services.processing_service import ProcessingService
from ui import ConsoleUI


class SystemConsoleUI(ConsoleUI):
    """Console UI with manual controls for the independent pipeline queues."""

    def __init__(self, processing_service=None, ai_service=None, **kwargs):
        super().__init__(**kwargs)
        self.processing_service = processing_service or ProcessingService()
        self.ai_service = ai_service or AIProcessingService()

    async def run_processing_queue(self):
        self.clear_screen()
        self.show_banner()
        self.show_section_header("Information Processing Queue")
        self.show_message("Checking the processing queue in the database...", Fore.CYAN)
        try:
            result = self.processing_service.process_pending_with_stats()
            self.show_message(
                f"Completed. Found: {result['found']} | "
                f"Processed: {result['processed']} | Failed: {result['failed']}",
                Fore.GREEN if result["failed"] == 0 else Fore.YELLOW,
            )
        except Exception as exc:
            self.show_message(f"Processing queue failed: {exc}", Fore.RED)
        await self.pause()

    async def run_ai_queue(self):
        self.clear_screen()
        self.show_banner()
        self.show_section_header("AI Processing Queue")
        self.show_message("Checking the AI queue in the database...", Fore.CYAN)
        try:
            result = self.ai_service.process_pending_with_stats()
            if result.get("disabled"):
                self.show_message("AI extraction is disabled in configuration.", Fore.YELLOW)
            else:
                self.show_message(
                    f"Completed. Found: {result['found']} | "
                    f"Processed: {result['processed']} | Failed: {result['failed']} | "
                    f"Skipped: {result['skipped']}",
                    Fore.GREEN if result["failed"] == 0 else Fore.YELLOW,
                )
        except Exception as exc:
            self.show_message(f"AI queue failed: {exc}", Fore.RED)
        await self.pause()

    async def run_groq_connection_test(self):
        self.clear_screen()
        self.show_banner()
        self.show_section_header("Groq Connection Test")
        try:
            success = test_groq_connection()
            self.show_message(
                "Groq minimal connection test succeeded."
                if success
                else "Groq minimal connection test failed. See diagnostic output above.",
                Fore.GREEN if success else Fore.RED,
            )
        except Exception as exc:
            self.show_message(f"Groq connection test failed: {exc}", Fore.RED)
        await self.pause()

    async def run(self):
        while True:
            self.clear_screen()
            self.show_banner()
            self.show_section_header("Main Menu")
            print(f"{Fore.GREEN}│  1. ▶️ Start scheduled crawler")
            print(f"{Fore.GREEN}│  2. 🧹 Process information queue")
            print(f"{Fore.GREEN}│  3. 🤖 Process AI queue")
            print(f"{Fore.GREEN}│  4. 🔬 Test Groq connection")
            print(f"{Fore.GREEN}│  5. ⚙️ Change settings")
            print(f"{Fore.GREEN}│  6. 📋 Manage channels")
            print(f"{Fore.GREEN}│  7. 👤 Switch / add account")
            print(f"{Fore.GREEN}│  8. 🚪 Exit")
            self.show_section_footer()

            choice = await self.prompt_choice(
                "\nChoose an option [1-8]: ",
                {"1", "2", "3", "4", "5", "6", "7", "8"},
            )
            if choice == "1":
                await self.start_crawler_flow()
            elif choice == "2":
                await self.run_processing_queue()
            elif choice == "3":
                await self.run_ai_queue()
            elif choice == "4":
                await self.run_groq_connection_test()
            elif choice == "5":
                await self.change_settings()
            elif choice == "6":
                await self.manage_channels()
            elif choice == "7":
                await self.account_menu()
            else:
                self.crawler.stop_all()
                await self.accounts.disconnect(self.client)
                self.client = None
                break
