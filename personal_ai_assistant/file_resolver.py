from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from .name_resolver import Entity, NameResolver, Resolution

# Navigation only: scripts, shortcuts and installers are never file candidates.
DOCUMENT_EXTENSIONS = {'.pdf', '.doc', '.docx', '.txt', '.md', '.csv', '.xlsx', '.xls',
                       '.ppt', '.pptx', '.rtf', '.odt', '.ods', '.png', '.jpg', '.jpeg'}


class FileResolver:
    def __init__(self, names: NameResolver, roots=(), enabled=False, timeout=2., max_files=10000):
        self.names, self.roots, self.enabled = names, tuple(Path(p) for p in roots), enabled
        self.timeout, self.max_files = timeout, max_files
        self.entities = []
        self.indexed_at = 0.

    def refresh(self):
        self.entities = []
        self.indexed_at = time.monotonic()
        if not self.enabled:
            return
        deadline = self.indexed_at + self.timeout
        seen = set()
        for root in self.roots:
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in {'node_modules', '__pycache__'}
                           and not (Path(directory) / d).is_symlink()]
                if time.monotonic() > deadline:
                    return
                for filename in files:
                    path = Path(directory) / filename
                    if path.suffix.lower() not in DOCUMENT_EXTENSIONS or path.is_symlink():
                        continue
                    key = str(path.absolute()).casefold()
                    if key in seen:
                        continue
                    try:
                        modified = path.stat().st_mtime
                    except OSError:
                        continue
                    seen.add(key)
                    self.entities.append(Entity('file:' + key, path.stem, 'file', str(path),
                                                aliases=(path.name,), modified=modified, context=str(path.parent)))
                    if len(self.entities) >= self.max_files or time.monotonic() > deadline:
                        return

    def candidates(self):
        if self.enabled and time.monotonic() - self.indexed_at > 60:
            self.refresh()
        return self.entities if self.enabled else []

    def resolve(self, query: str, context='') -> Resolution:
        entities = self.candidates()
        original = self.names.resolve(query, entities, context)
        if original.best:
            return original
        if '昨天' in query:
            yesterday = datetime.now().date() - timedelta(days=1)
            dated = [e for e in entities if datetime.fromtimestamp(e.modified).date() == yesterday
                     or datetime.fromtimestamp(self.names.store.data['usage'].get(e.id, {}).get('at', 0)).date() == yesterday]
            if dated:
                entities = dated
        cleaned = re.sub(r'昨天|最近|最新|那個', '', query).strip()
        result = self.names.resolve(cleaned or query, entities, context)
        # Version/back-up families always ask unless an explicit learned mapping exists.
        return Resolution(query, result.candidates, result.needs_confirmation)

    @staticmethod
    def open(entity: Entity):
        path = Path(entity.target)
        if entity.kind != 'file' or path.suffix.lower() not in DOCUMENT_EXTENSIONS or not path.is_file():
            return False
        try:
            os.startfile(str(path))
            return True
        except OSError:
            return False
