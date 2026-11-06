import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import pytest


@pytest.fixture
def mm(tmp_path, monkeypatch):
    import config
    import routing_rules
    from types import SimpleNamespace
    from message_manager.repository import Repository
    from message_manager.media_service import MediaService
    from message_manager.service import Service
    from message_manager.excel_service import ExcelService
    from message_manager.models import SendResult

    monkeypatch.setattr(config, 'DB_NAME', str(tmp_path / 'mm.sqlite'))
    monkeypatch.setattr(config, 'MESSAGE_MANAGER_MEDIA_PATH', str(tmp_path / 'private'))
    routing_rules.initialize()
    routing_rules.save_target('First', '@firstchannel', enabled=False)
    routing_rules.save_target('Second', '@secondchannel')
    repo = Repository()
    repo.initialize()
    repo.eligible(1, True, 42)
    repo.eligible(2, True, 42)
    media = MediaService(repo)

    class Sender:
        def __init__(self):
            self.sent = []
            self.checked = []
            self.result = SendResult('sent', message_id=123, message_link='https://t.me/firstchannel/123')
            self.media = media

        async def check_target(self, target):
            self.checked.append(target['id'])
            return target['label']

        async def send(self, job):
            self.sent.append(job)
            return self.result

    sender = Sender()
    service = Service(repo, sender)
    return SimpleNamespace(repo=repo, sender=sender, service=service, media=media, excel=ExcelService(service))
