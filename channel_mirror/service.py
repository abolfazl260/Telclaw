"""Boundary for the future crawl hook. This service never sends messages."""

from typing import Protocol

from .models import SourceItem, SourcePost


class MirrorRepository(Protocol):
    def enqueue(self, post: SourcePost, *, now=None) -> list[int]: ...


class Service:
    def __init__(self, repository: MirrorRepository):
        self.repository = repository

    def observe_media(self, source_peer_id: int, items: tuple[SourceItem, ...],
                      crawl_run_id: str, *, album_complete=False, now=None) -> list[int]:
        """Persist one complete post and return only newly queued delivery IDs.

        The caller must assemble the entire album before passing album_complete.
        Source text, sender identity, buttons, and AI results are not inputs.
        """
        post = SourcePost(source_peer_id, items, crawl_run_id, album_complete)
        return self.repository.enqueue(post, now=now)
