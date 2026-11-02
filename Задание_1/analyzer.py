import csv
import re
import sqlite3
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
DB = FOLDER / 'data' / 'articles.sqlite3'
OUT = FOLDER / 'results'
WEIGHTS = {
    'Хактивисты': (35, 20, 10, 35),
    'Киберпреступники': (30, 20, 30, 20),
    'APT': (10, 30, 35, 25),
}
KEYWORDS = {
    'Хактивисты': ['hacktivist', 'hacktivism', 'хактивист', 'politically motivated'],
    'Киберпреступники': ['ransomware', 'extortion', 'cybercriminal', 'phishing', 'infostealer', 'stolen data', 'фишинг'],
    'APT': ['apt28', 'apt29', 'apt41', 'apt42', 'espionage', 'state-linked', 'state-sponsored', 'state-aligned', 'nation-state', 'шпионаж'],
}

EVENT_WORDS = [
    'attack', 'attacks', 'cyberattack', 'breach', 'breached',
    'compromise', 'compromised', 'hacked', 'hackers', 'ransomware',
    'extortion', 'phishing', 'credential', 'stolen', 'leak', 'malware',
    'vulnerability', 'espionage', 'intrusion', 'backdoor',
    'атак', 'взлом', 'утеч', 'уязвим', 'вымогател', 'фишинг',
]
FIELDS = ['company', 'category', 'source', 'date', 'title', 'url', 'confirmed', 'factor', 'comment']

def read_csv(path):
    with open(path, encoding='utf-8-sig', newline='') as file:
        return list(csv.DictReader(file))

def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

def mentioned(text, aliases):
    for name in aliases.split('|'):
        name = name.strip()
        if not name:
            continue
        flags = 0 if name in ('VK', 'AWS') else re.IGNORECASE
        if re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text, flags):
            return True
    return False

def get_articles():
    if not DB.exists():
        raise FileNotFoundError('Не найдена база. Сначала запусти python3 collector.py --search')
    with sqlite3.connect(DB) as db:
        return db.execute('SELECT source, title, summary, url, published_at FROM articles').fetchall()

def get_candidates(articles, companies):

    result = []
    used = set()
    for source, title, summary, url, date in articles:
        text = (title + ' ' + summary).lower()
        if not any(word in text for word in EVENT_WORDS):
            continue
        for company in companies:
            if not mentioned(title + ' ' + summary, company['aliases']):
                continue
            key = (company['company'], url)
            if key in used:
                continue
            used.add(key)

            categories = [category for category, words in KEYWORDS.items()
                          if any(word in text for word in words)]
            category = categories[0] if len(categories) == 1 else 'Не определено'
            result.append(dict(company=company['company'], category=category,
                               source=source, date=date, title=title, url=url,
                               confirmed='', factor='', comment=''))
    return result

def update_review(candidates):

    path = OUT / 'review.csv'
    old = {}
    if path.exists():
        for item in read_csv(path):

            old[(item['company'], item['url'])] = item
    for item in candidates:
        key = (item['company'], item['url'])
        if key not in old:
            old[key] = item
    write_csv(path, FIELDS, list(old.values()))
    return list(old.values())

def analyze(candidates, companies, review):

    available = {(r["company"], r["url"]) for r in candidates}
    rating = []
    evidence = []
    for company in companies:
        for category, weights in WEIGHTS.items():
            approved = []
            for item in review:
                if item['company'] != company['company'] or item['category'] != category:
                    continue
                if (item['company'], item['url']) not in available:
                    continue
                if item['confirmed'].strip().lower() != 'да':
                    continue
                factor = item['factor'].strip().lower()
                if factor not in ('интерес', 'атака', 'событие'):
                    continue
                approved.append(item)
                evidence.append(item)

            direct = min(5, 2 * sum(x['factor'].lower() == 'интерес' for x in approved))
            history = min(5, 2 * sum(x['factor'].lower() == 'атака' for x in approved))
            events = min(5, 2 * sum(x['factor'].lower() == 'событие' for x in approved))
            profile = int(company[category])
            if approved:
                score = round((direct * weights[0] + history * weights[1]
                               + profile * weights[2] + events * weights[3]) / 5, 1)
                status = 'предварительная, низкая уверенность'
            else:
                score = ''
                status = 'недостаточно проверенных данных'
            rating.append(dict(company=company['company'], category=category, score=score,
                               status=status, direct=direct, history=history,
                               profile=profile, events=events, articles=len(approved)))
    return rating, evidence

def main():
    try:
        articles = get_articles()
        companies = read_csv(FOLDER / 'companies.csv')
        candidates = get_candidates(articles, companies)
        review = update_review(candidates)
        rating, evidence = analyze(candidates, companies, review)
    except (OSError, sqlite3.Error, ValueError) as error:
        print('[ОШИБКА]', error)
        return

    rating_fields = ['company', 'category', 'score', 'status', 'direct', 'history',
                     'profile', 'events', 'articles']
    write_csv(OUT / 'rating.csv', rating_fields, rating)
    write_csv(OUT / 'evidence.csv', FIELDS, evidence)

    print('Всего публикаций:', len(articles))
    print('Найдено потенциально подходящих публикаций:', len(candidates))
    print('Строк в review.csv (включая прошлые):', len(review))
    print('Подтверждённых записей из текущей базы:', len(evidence))
    print('')
    for category in WEIGHTS:
        print(category + ':')
        rows = [x for x in rating if x['category'] == category and x['score'] != '']
        if not rows:
            print('  Недостаточно проверенных данных')
        for item in sorted(rows, key=lambda x: x['score'], reverse=True):
            print(f"  {item['company']}: {item['score']} балла, публикаций: {item['articles']}")
    print('Результаты сохранены в папке results.')

if __name__ == '__main__':
    main()
