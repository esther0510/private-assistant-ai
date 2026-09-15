"""Known desktop clients; helpers/update wrappers never count as main clients."""
from dataclasses import dataclass

@dataclass(frozen=True)
class AppProfile:
    name: str
    processes: tuple[str, ...]
    aliases: tuple[str, ...]
    restore_uri: str = ''

PROFILES = (
    AppProfile('LINE', ('line.exe',), ('line', '賴', '賴恩', '萊恩', '來因')),
    AppProfile('Steam', ('steam.exe',), ('steam', '斯蒂姆', '史汀', '史丁', '斯汀'), 'steam://open/main'),
    AppProfile('Opera', ('opera.exe',), ('opera', '歐佩拉', '歐培拉', '歐普拉', 'op啦')),
    AppProfile('Telegram', ('telegram.exe',), ('telegram', '電報', '特勒格拉姆')),
)

def app_profile(query):
    key = query.strip().casefold().removesuffix('.exe')
    return next((p for p in PROFILES if key in p.aliases), None)

def process_profile(process):
    return next((p for p in PROFILES if process.casefold() in p.processes), None)
