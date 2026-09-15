"""Local, confidence-aware entity ranking. No network or command execution."""
from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .storage import default_data_dir


def normalize_name(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKC', re.sub('[™®©]', '', text)).casefold() if c.isalnum())


def phonetic_key(text: str) -> str:
    text = normalize_name(text)
    for source, target in (("佛頭夏普", "photoshop"), ("佛陀夏普", "photoshop"), ("福頭夏普", "photoshop")):
        text = text.replace(source, target)
    text = text.replace('ph', 'f').replace('sh', 's').replace('ch', 's').replace('ck', 'k')
    return re.sub(r'([a-z])\1+', r'\1', re.sub(r'[aeiouy]', '', text))


BUILTIN_NAMES = {
    'telegram': ('tele gram', '特勒格拉姆', '泰勒格拉姆', '電報'),
    'opera': ('op啦', '歐佩拉', '歐培拉', '歐普拉'),
    'photoshop': ('PS', 'P圖', 'photo shop', '佛頭夏普', '修圖軟體'),
    'premiere': ('PR', '剪影片的', '剪片的'),
    'discord': ('小黑', '迪斯科德', '迪斯扣', 'dis cord'),
    'steam': ('斯蒂姆', '史汀', '史丁', '斯汀'),
    'spotify': ('斯波提菲', '斯破提派', '史波提菲'),
    'chrome': ('克羅姆', '谷歌瀏覽器'),
    'chatgpt': ('恰特GPT', '洽特GPT'),
    'unity': ('優尼提', '尤尼提'),
    'stablediffusionforge': ('Forge', '生圖', '畫圖'),
    'helldivers2': ('HD2', 'hell device', 'helldiver', 'hell divers', 'hell divers 2', '絕地戰兵', '絕地戰兵2', '那個蟲子遊戲'),
}


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    kind: str
    target: str
    provider: str = ''
    aliases: tuple[str, ...] = ()
    modified: float = 0
    context: str = ''
    metadata: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class RankedEntity:
    entity: Entity
    confidence: float
    reason: str


@dataclass(frozen=True)
class Resolution:
    query: str
    candidates: tuple[RankedEntity, ...]
    needs_confirmation: bool

    @property
    def best(self) -> Entity | None:
        return self.candidates[0].entity if self.candidates and not self.needs_confirmation else None


class AliasStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_data_dir() / 'entity_memory.json'
        try:
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.data, dict):
                raise ValueError('Invalid memory')
        except (OSError, ValueError):
            self.data = {}
        for key in ('aliases', 'usage', 'preferences'):
            if not isinstance(self.data.get(key), dict):
                self.data[key] = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.path)

    def confirm(self, query: str, entity: Entity):
        key = normalize_name(query)
        if not key:
            return
        self.data['aliases'][key] = {'query': query, 'id': entity.id, 'name': entity.name,
                                    'kind': entity.kind, 'target': entity.target, 'provider': entity.provider}
        if entity.kind == 'game':
            self.data['preferences'][normalize_name(entity.name)] = entity.provider
        self.save()

    def remove(self, key: str):
        item = self.data['aliases'].pop(key, None)
        if item and item.get('kind') == 'game':
            self.data['preferences'].pop(normalize_name(item['name']), None)
        self.save()

    def used(self, entity: Entity):
        previous = self.data['usage'].get(entity.id, {})
        self.data['usage'][entity.id] = {'count': previous.get('count', 0) + 1, 'at': time.time()}
        self.save()


class NameResolver:
    def __init__(self, store: AliasStore | None = None):
        self.store = store or AliasStore()

    def resolve(self, query: str, entities, context: str = '') -> Resolution:
        normalized = normalize_name(query)
        if not normalized:
            return Resolution(query, (), False)
        remembered = self.store.data['aliases'].get(normalized, {}).get('id')
        ranked = []
        for entity in {e.id: e for e in entities}.values():
            name = normalize_name(entity.name.removesuffix('.exe'))
            aliases = list(entity.aliases)
            for token, values in BUILTIN_NAMES.items():
                if token in name or (token == 'stablediffusionforge' and 'forge' in name):
                    aliases.extend(values)
            if entity.id == remembered:
                score, reason = 1.0, 'confirmed alias'
            elif normalized == name or normalized in [normalize_name(a) for a in entity.aliases]:
                score, reason = .99, 'exact'
            else:
                score, reason = 0.0, 'fuzzy / phonetic'
                for value in (entity.name, *aliases):
                    key = normalize_name(value.removesuffix('.exe'))
                    if not key:
                        continue
                    match = SequenceMatcher(None, normalized, key).ratio() * .88
                    if normalized == key:
                        # Descriptive defaults are suggestions until explicitly selected.
                        match = .96 if len(normalized) <= 3 or normalized.isascii() or any(t in name for t in ('telegram', 'opera')) else .84
                    elif normalized in key and len(normalized) >= 2:
                        match = max(match, .87)
                    if phonetic_key(query) and phonetic_key(query) == phonetic_key(value):
                        match = max(match, .92)
                    spoken_key, candidate_key = phonetic_key(query), phonetic_key(value)
                    if spoken_key and candidate_key:
                        match = max(match, SequenceMatcher(None, spoken_key, candidate_key).ratio() * .88)
                    tokens = re.findall(r'[A-Za-z0-9]+', value)
                    query_tokens = re.findall(r'[A-Za-z0-9]+', query.casefold())
                    if query_tokens and tokens:
                        token_score = SequenceMatcher(None, ' '.join(sorted(query_tokens)), ' '.join(sorted(t.casefold() for t in tokens))).ratio() * .88
                        match = max(match, token_score)
                    if len(tokens) > 1 and normalized == ''.join(t[0].lower() for t in tokens):
                        match = max(match, .86)
                    score = max(score, match)
            # Descriptive non-Latin game defaults are suggestions, not verified titles.
            if (entity.kind == 'game' and not normalized.isascii() and entity.id != remembered
                    and normalized != name and normalized not in [normalize_name(a) for a in entity.aliases]):
                score = min(score, .84)
            if score < .40:
                continue
            if entity.kind in {'window', 'appclient'} and score < .98:
                score += .02
            usage = self.store.data['usage'].get(entity.id, {})
            if score < .98:
                score += min(.025, math.log1p(usage.get('count', 0)) * .007)
                score += .02 * math.exp(-max(0, time.time() - usage.get('at', 0)) / 86400)
                if context and normalize_name(context) in normalize_name(entity.context):
                    score += .025
                if entity.kind == 'file':
                    score += .02 * math.exp(-max(0, time.time() - entity.modified) / 86400)
            preferred = self.store.data['preferences'].get(normalize_name(entity.name))
            if entity.kind == 'game' and preferred == entity.provider and score >= .80:
                score = 1.0
            ranked.append(RankedEntity(entity, min(1., score), reason))
        ranked.sort(key=lambda item: item.confidence, reverse=True)
        if not ranked:
            return Resolution(query, (), False)
        best = ranked[0]
        ambiguous = len(ranked) > 1 and best.confidence - ranked[1].confidence < .10
        if best.reason == 'exact' and (len(ranked) == 1 or ranked[1].confidence < .98):
            ambiguous = False
        same_game = best.entity.kind == 'game' and any(
            normalize_name(r.entity.name) == normalize_name(best.entity.name) and r.entity.provider != best.entity.provider
            for r in ranked[1:])
        if same_game and self.store.data['preferences'].get(normalize_name(best.entity.name)) == best.entity.provider:
            ambiguous = False
        elif same_game:
            ambiguous = True
        if best.entity.id == remembered:
            ambiguous = False
        return Resolution(query, tuple(ranked[:5]), ambiguous or best.confidence < .90)
