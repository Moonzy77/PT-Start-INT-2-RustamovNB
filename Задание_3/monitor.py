import argparse
import csv
from datetime import datetime
from email.utils import parsedate_to_datetime
import re
import sqlite3
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
SOURCES = {
    'BleepingComputer': 'https://www.bleepingcomputer.com/feed/',
    'The Record': 'https://therecord.media/feed',
    'Securelist': 'https://securelist.com/feed/',
}

COMPANIES = {
    'ConnectWise': ['connectwise', 'screenconnect'],
    'Microsoft': ['microsoft', 'microsoft 365', 'm365'],
    'Cisco': ['cisco'], 'VK': ['вконтакте', 'vk id', 'vk cloud'],
    'Okta': ['okta'], 'GitLab': ['gitlab'], 'Cloudflare': ['cloudflare'],
}
# Правила применяются к заголовку в порядке приоритета.
# Границы слов исключают ложные совпадения: например, RCE в слове source.
RULES = [
    ('Продажа доступа', [r'\baccess for sale\b', r'\bselling access\b',
                         r'\binitial access brokers?\b', r'продаж\w* доступ', r'доступ\w* прода\w*']),
    ('Вымогательство', [r'\bransomware\b', r'\bransom demand\b', r'\bextortion\b',
                        r'вымогател\w*', r'шифровальщик\w*']),
    ('Утечка данных', [r'\bdata breaches?\b', r'\bdata leaks?\b',
                       r'\bleaked data\b', r'\bstolen (?:data|records|information)\b',
                       r'утечк\w*', r'слив\w* данн\w*']),
    ('Заявление об атаке', [r'\bclaims? responsibility\b', r'\bclaim(?:ed|s)? (?:the )?attack\b',
                            r'\bhacktivis\w*\b', r'ответственност\w* за атак\w*']),
    ('Фишинг', [r'\bphishing\b', r'\bvishing\b', r'\bsmishing\b', r'фишинг\w*']),
    ('Уязвимость', [r'\bcve-\d{4}-\d{4,}\b', r'\bvulnerabilit(?:y|ies)\b',
                     r'\b(?:zero|0)[- ]?day\b', r'\brce\b', r'\bremote code execution\b',
                     r'\bsecurity flaw\b', r'\bexploit(?:ed|ation)? (?:bug|flaw|vulnerability)\b',
                     r'уязвимост\w*']),
    ('Инцидент / другое', [r'\bapt\d*\b', r'\b(?:hackers?|attackers?|threat actors?)\b',
                            r'\b(?:cyberattacks?|attacks?|attacked|targeted|targeting)\b',
                            r'\b(?:breached|breaches|compromised|compromise|hacked|hacking)\b',
                            r'\b(?:malware|trojan|botnet|bootkit|backdoor|spyware|stealer)\b',
                            r'\b(?:intrusion|infiltration|supply[- ]chain attack)\b',
                            r'\b(?:credential theft|stolen credentials|security incidents?)\b',
                            r'\b(?:взлом\w*|атак\w*|компрометац\w*|вредонос\w*)\b']),
]

# Для аннотаций допускаются только более конкретные признаки.
# Это снижает риск пометить обзорную статью как инцидент из-за фонового слова "attack".
DESCRIPTION_RULES = RULES[:-1] + [
    ('Инцидент / другое', [r'\bapt\d*\b', r'\b(?:hacked|breached|compromised|malware|bootkit|trojan|botnet)\b',
                            r'\b(?:threat actors?|attackers?)\b']),
]

def classify(title, description=''):
    for label, patterns in RULES:
        if any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in patterns):
            return label
    for label, patterns in DESCRIPTION_RULES:
        if any(re.search(pattern, description[:350], flags=re.IGNORECASE) for pattern in patterns):
            return label
    return 'Другое'

STOP = {'with', 'from', 'after', 'that', 'this', 'their', 'through', 'about',
        'attack', 'attacks', 'cyberattack', 'hackers', 'security', 'data', 'the', 'and',
        'says', 'reported', 'report', 'new', 'for', 'are', 'via', 'cloud', 'service'}

def local(tag):
    return tag.rsplit('}', 1)[-1].lower()

def child_text(item, names):
    for ch in item:
        if local(ch.tag) in names:
            return ' '.join(ch.itertext()).strip()
    return ''

def iso_date(value):
    if not value:
        return ''
    try:
        date = parsedate_to_datetime(value)
    except (ValueError, TypeError):
        try:
            date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return ''
    return date.date().isoformat()

def read_feed(data, source):
    root = ET.fromstring(data)
    result = []
    for item in root.iter():
        if local(item.tag) not in ('item', 'entry'):
            continue
        title = child_text(item, {'title'})
        link = child_text(item, {'link'})
        if not link:
            for part in item:
                if local(part.tag) == 'link' and part.attrib.get('href'):
                    link = part.attrib['href']
                    break
        if not title or not link.startswith(('https://', 'http://')):
            continue
        description = child_text(item, {'description', 'summary', 'content'})
        description = re.sub(r'<[^>]+>', ' ', description)
        result.append({
            'source': source, 'date': iso_date(child_text(item, {'pubdate', 'published', 'updated', 'date'})),
            'author': child_text(item, {'creator', 'author'}) or 'не указан',
            'title': title, 'text': re.sub(r'\s+', ' ', description).strip()[:800],
            'url': link,
        })
    return result

def read_old_db(path):
    with sqlite3.connect(path) as db:
        return [dict(source=s, date=iso_date(d), author='не указан', title=t,
                     text=summary or '', url=u)
                for s, t, summary, u, d in db.execute(
                    'SELECT source,title,summary,url,published_at FROM articles')]

def read_publications(path):

    with path.open(encoding='utf-8-sig', newline='') as f:
        return [dict(source=r['source'], date=iso_date(r['date']), author=r.get('author') or 'не указан',
                     title=r['title'], text=r.get('text') or '', url=r['url']) for r in csv.DictReader(f)]

def analyze(title, description=''):
    text_low = (title + ' ' + description).casefold()
    category = classify(title, description)
    found = []
    for company, aliases in COMPANIES.items():
        if any(re.search(r'(?<!\w)' + re.escape(a) + r'(?!\w)', text_low)
               for a in aliases):
            found.append(company)

    domains = sorted(set(re.findall(r'(?<![\w.-])(?:[a-z0-9-]+\.)+(?:ru|com|net|org|io|рф)\b', text_low)))
    return category, found, domains

def tokens(title):
    return {w for w in re.findall(r'[a-zа-яё0-9]{4,}', title.casefold())
            if w not in STOP}

def same_event(article, first):

    if article['category'] == 'Другое' or first['category'] == 'Другое':
        return False
    if article['category'] != first['category']:
        return False
    shared = set(article['companies']) & set(first['companies'])
    if not shared:
        return False
    if article['date'] and first['date']:
        gap = abs((datetime.fromisoformat(article['date']) - datetime.fromisoformat(first['date'])).days)
        if gap > 10:
            return False
    a, b = tokens(article['title']), tokens(first['title'])
    return len(a & b) >= 2 and len(a & b) / max(1, len(a | b)) >= 0.20

def process(publications):

    unique, keys = [], set()
    for original in publications:
        url = original['url'].strip()
        title = re.sub(r'\s+', ' ', original['title']).strip()
        if not url or not title:
            continue

        source_group = 'Google News' if original['source'].startswith('GoogleNews:') else original['source']
        key1, key2 = ('url', url), ('title', source_group, title.casefold())
        if key1 in keys or key2 in keys:
            continue
        keys.update((key1, key2))
        row = dict(original, url=url, title=title)
        category, companies, domains = analyze(title, row['text'])
        row.update(category=category, companies=companies, domains=domains)
        unique.append(row)
    unique.sort(key=lambda r: (r['date'], r['url']))

    groups = []
    for row in unique:
        group = next((g for g in groups if same_event(row, g[0])), None)
        if group is None:
            groups.append([row])
        else:
            group.append(row)
    return unique, groups

def publisher(article):
    """По заголовку Google News определяем указанного издателя, если он есть."""
    if article['source'].startswith('GoogleNews:'):
        parts = article['title'].rsplit(' - ', 1)
        if len(parts) == 2 and 1 < len(parts[1].strip()) < 80:
            return parts[1].strip()
        return 'Google News (издатель не указан)'
    return article['source']

def save_csv(filename, fields, rows, folder):
    with (folder / filename).open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

def save_results(articles, groups, folder):
    folder.mkdir(parents=True, exist_ok=True)
    event_rows = []
    for n, group in enumerate(groups, 1):

        sources = sorted({publisher(r) for r in group})
        companies = sorted({c for r in group for c in r['companies']})

        priority = (0 if group[0]['category'] == 'Другое' else min(5,
                    1 + int(bool(companies)) + int(len(sources) > 1)
                    + int(group[0]['category'] in ('Утечка данных', 'Продажа доступа', 'Вымогательство'))))
        event_rows.append(dict(event_id=n, category=group[0]['category'],
                               companies=', '.join(companies), first_seen=group[0]['date'],
                               first_source=publisher(group[0]), sources=len(sources),
                               posts=len(group), priority=priority, example_url=group[0]['url']))
        for r in group:
            r['event_id'] = n
    post_rows = [dict(source=r['source'], publisher=publisher(r), date=r['date'], author=r['author'], title=r['title'],
                      text=r['text'], url=r['url'], category=r['category'],
                      companies=', '.join(r['companies']), domains=', '.join(r['domains']),
                      event_id=r['event_id']) for r in articles]
    post_fields = ['source','publisher','date','author','title','text','url','category','companies','domains','event_id']
    event_fields = ['event_id','category','companies','first_seen','first_source','sources','posts','priority','example_url']
    save_csv('posts.csv', post_fields, post_rows, folder)
    save_csv('events.csv', event_fields, event_rows, folder)
    days = {}
    for r in articles:
        days[r['date'] or 'дата не указана'] = days.get(r['date'] or 'дата не указана', 0) + 1
    save_csv('trend.csv', ['date','posts'],
             [dict(date=d, posts=n) for d,n in sorted(days.items())], folder)
    with sqlite3.connect(folder / 'monitor.sqlite3') as db:
        db.execute('DROP TABLE IF EXISTS posts')
        db.execute('DROP TABLE IF EXISTS events')
        db.execute('CREATE TABLE posts (source TEXT, publisher TEXT, date TEXT, author TEXT, title TEXT, text TEXT, url TEXT, category TEXT, companies TEXT, domains TEXT, event_id INTEGER)')
        db.execute('CREATE TABLE events (event_id INTEGER, category TEXT, companies TEXT, first_seen TEXT, first_source TEXT, sources INTEGER, posts INTEGER, priority INTEGER, example_url TEXT)')
        db.executemany('INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       ([r[f] for f in post_fields] for r in post_rows))
        db.executemany('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)',
                       ([r[f] for f in event_fields] for r in event_rows))
    return event_rows

def main():
    p = argparse.ArgumentParser(description='Пассивный мониторинг новостей по ИБ')
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--live', action='store_true', help='Загрузить открытые RSS')
    source.add_argument('--from-db', type=Path, help='Использовать базу статей из задания №1')
    source.add_argument('--from-csv', '--from-review', dest='from_review', type=Path, help='Обработать сохранённые публикации из CSV')
    p.add_argument('--out', type=Path, default=HERE / 'results', help='Папка результатов')
    args = p.parse_args()
    posts = []
    if args.from_db:
        posts = read_old_db(args.from_db)
        print('Режим: ранее собранные публикации')
    elif args.from_review:
        posts = read_publications(args.from_review)
        print('Режим: публикации из CSV')
    else:
        for name, url in SOURCES.items():
            try:
                request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (student RSS research)'})
                with urlopen(request, timeout=15) as f:
                    data = f.read(2_000_001)
                if len(data) > 2_000_000:
                    raise ValueError('слишком большой ответ')
                batch = read_feed(data, name)
                posts.extend(batch)
                print(name + ':', len(batch))
            except (OSError, ValueError, ET.ParseError) as exc:
                print(name + ': ошибка получения -', exc)
    unique, groups = process(posts)
    events = save_results(unique, groups, args.out)
    print(f'Получено: {len(posts)}; после удаления повторов: {len(unique)}; групп сообщений: {len(events)}')
    print('Группы с двумя и более указанными издателями:', sum(r['sources'] > 1 for r in events))
    print('Результаты:', args.out.resolve())

if __name__ == '__main__':
    main()
