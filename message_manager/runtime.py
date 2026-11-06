from types import SimpleNamespace
import routing_rules
from .repository import Repository
from .media_service import MediaService
from .telegram_sender import TelegramSender
from .service import Service
from .scheduler import Worker
from .excel_service import ExcelService


def build_runtime():
    routing_rules.initialize()
    repo = Repository()
    repo.initialize()
    media = MediaService(repo)
    sender = TelegramSender(media)
    service = Service(repo, sender)
    return SimpleNamespace(repo=repo, media=media, sender=sender, service=service,
                           worker=Worker(repo, sender), excel=ExcelService(service))
