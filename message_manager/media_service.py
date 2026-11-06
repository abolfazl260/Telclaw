"""Private, bounded streaming uploads with immutable generated paths."""
import hashlib
import os
import uuid
from datetime import timedelta
from pathlib import Path
from zipfile import is_zipfile

from PIL import Image, UnidentifiedImageError
import config
from .models import stamp, utcnow
from .validation import ValidationError

TYPES = {
    '.jpg': ('photo', 'image/jpeg'), '.jpeg': ('photo', 'image/jpeg'), '.png': ('photo', 'image/png'),
    '.mp4': ('video', 'video/mp4'), '.pdf': ('document', 'application/pdf'),
    '.txt': ('document', 'text/plain'), '.zip': ('document', 'application/zip'),
    '.docx': ('document', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    '.xlsx': ('document', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
}


class MediaService:
    def __init__(self, repository, root=None, max_bytes=None):
        self.repo = repository
        self.root = Path(root or config.MESSAGE_MANAGER_MEDIA_PATH).resolve()
        self.max_bytes = min(max_bytes or config.MESSAGE_MANAGER_MEDIA_MAX_BYTES, 50 * 1024 * 1024)

    def path(self, media):
        filename = media['filename']
        if Path(filename).name != filename:
            raise ValidationError('Invalid media path')
        path = (self.root / filename).resolve()
        if path.parent != self.root or not path.is_file() or path.is_symlink():
            raise ValidationError('Media file is missing or outside private storage')
        return path

    async def upload(self, name, mime, chunks, actor):
        name = str(name).replace('\\', '/').split('/')[-1][:200]
        extension = Path(name).suffix.lower()
        if extension not in TYPES:
            raise ValidationError('Allowed uploads: JPEG, PNG, MP4, PDF, TXT, ZIP, DOCX, XLSX')
        kind, expected = TYPES[extension]
        if mime.split(';')[0].lower() != expected:
            raise ValidationError('Content type does not match the file extension')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        filename = uuid.uuid4().hex + extension
        path = self.root / filename
        size, digest = 0, hashlib.sha256()
        limit = min(self.max_bytes, 10 * 1024 * 1024) if kind == 'photo' else self.max_bytes
        try:
            with path.open('xb') as out:
                os.chmod(path, 0o600)
                async for chunk in chunks:
                    size += len(chunk)
                    if size > limit:
                        raise ValidationError(f'Upload exceeds {limit} bytes')
                    digest.update(chunk)
                    out.write(chunk)
            if not size:
                raise ValidationError('Empty upload')
            self._inspect(path, kind, extension)
            key = uuid.uuid4().hex
            with self.repo.connection(True) as c:
                c.execute('INSERT INTO mm_media(asset_key,filename,original_name,kind,mime,size,sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                          (key, filename, name, kind, expected, size, digest.hexdigest(), actor, stamp()))
                self.repo.audit(c, actor, 'media_upload', key, digest.hexdigest())
            return self.repo.media(key)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _inspect(path, kind, extension):
        try:
            with path.open('rb') as stream:
                head = stream.read(32)
            if kind == 'photo':
                with Image.open(path) as im:
                    w, h = im.size
                    if im.format != ('PNG' if extension == '.png' else 'JPEG') or w + h > 10000 or max(w, h) / min(w, h) > 20:
                        raise ValidationError('Photo dimensions/format exceed Telegram limits')
                    im.verify()
            elif kind == 'video' and (len(head) < 12 or head[4:8] != b'ftyp'):
                raise ValidationError('Expected an MP4 video')
            elif extension == '.pdf' and not head.startswith(b'%PDF-'):
                raise ValidationError('Expected a PDF document')
            elif extension in {'.zip', '.docx', '.xlsx'} and not is_zipfile(path):
                raise ValidationError('Expected a ZIP/Office document')
            elif extension == '.txt':
                # Bounded decode validation; no executable HTML documents are served inline.
                with path.open('r', encoding='utf-8') as stream:
                    while stream.read(65536):
                        pass
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ValidationError('Invalid media contents') from exc

    def purge_unreferenced(self, retention_days):
        cutoff = stamp(utcnow() - timedelta(days=retention_days))
        with self.repo.connection(True) as c:
            assets = c.execute('''SELECT * FROM mm_media m WHERE created_at<?
              AND NOT EXISTS(SELECT 1 FROM mm_message_media x WHERE x.asset_key=m.asset_key)
              AND NOT EXISTS(SELECT 1 FROM mm_schedules s WHERE json_extract(s.snapshot_json,'$.asset_key')=m.asset_key)
              AND NOT EXISTS(SELECT 1 FROM mm_import_batches b WHERE b.state='preview' AND b.preview_json LIKE '%' || m.asset_key || '%') LIMIT 100''', (cutoff,)).fetchall()
            for asset in assets:
                try:
                    self.path(asset).unlink()
                except ValidationError:
                    pass
                c.execute('DELETE FROM mm_media WHERE asset_key=?', (asset['asset_key'],))
            return len(assets)
