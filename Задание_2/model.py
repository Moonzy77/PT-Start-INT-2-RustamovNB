import csv
from collections import deque
from html import escape
from pathlib import Path
from textwrap import wrap

FOLDER = Path(__file__).resolve().parent
RESULTS = FOLDER / 'results'


def read_table(filename):
    with open(FOLDER / filename, encoding='utf-8-sig', newline='') as file:
        return list(csv.DictReader(file))


def find_affected(start, links):
    affected = [start]
    for node in affected:
        for link in links:
            if link['source'] == node and link['target'] not in affected:
                affected.append(link['target'])
    return affected


def draw_graph(nodes, links, scenarios, results):
    names = {node['id']: node['name'] for node in nodes}
    neighbors = {node: [] for node in names}
    for link in links:
        neighbors[link['source']].append(link['target'])
        neighbors[link['target']].append(link['source'])

    components = []
    visited = set()
    starts = [scenario['root'] for scenario in scenarios] + list(names)
    for start in starts:
        if start in visited:
            continue
        levels = {start: 0}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for other in neighbors[current]:
                if other not in levels:
                    levels[other] = levels[current] + 1
                    queue.append(other)
        visited.update(levels)
        components.append((start, levels))

    box_width = 275
    step_x = 330
    step_y = 76
    max_depth = max((max(levels.values()) for _, levels in components), default=0)
    width = max(820, 65 + (max_depth + 1) * step_x)
    parts = []
    y = 16

    for start, levels in components:
        columns = {}
        for node in names:
            if node in levels:
                columns.setdefault(levels[node], []).append(node)
        height = 83 + max(len(col) for col in columns.values()) * step_y
        position = {}
        for depth, column in columns.items():
            for index, node in enumerate(column):
                position[node] = (30 + depth * step_x, y + 64 + index * step_y)

        parts.append(f'<rect x="12" y="{y}" width="{width-24}" height="{height}" rx="12" fill="#f8fafc" stroke="#cbd5e1"/>')
        heading = names[start]
        related = [s for s in scenarios if s['root'] in levels]
        if related:
            heading = ' / '.join(s['name'] for s in related)
        parts.append(f'<text x="30" y="{y+30}" font-size="17" font-weight="bold" fill="#1e293b">{escape(heading)}</text>')

        for link in links:
            if link['source'] not in position or link['target'] not in position:
                continue
            x1, y1 = position[link['source']]
            x2, y2 = position[link['target']]
            if x2 > x1:
                sx, tx = x1 + box_width + 3, x2 - 6
                sy, ty = y1 + 27, y2 + 27
            elif x2 < x1:
                sx, tx = x1 - 3, x2 + box_width + 6
                sy, ty = y1 + 27, y2 + 27
            else:
                sx, tx = x1 + box_width / 2, x2 + box_width / 2
                sy, ty = y1 + (56 if y2 > y1 else 0), y2 + (0 if y2 > y1 else 56)
            parts.append(f'<path d="M{sx},{sy} L{tx},{ty}" stroke="#475569" stroke-width="1.8" fill="none" marker-end="url(#arrow)"/>')

        for node, (x, top) in position.items():
            parts.append(f'<rect x="{x}" y="{top}" width="{box_width}" height="56" rx="8" fill="white" stroke="#64748b"/>')
            lines = wrap(names[node], width=32, break_long_words=False) or ['']
            lines = lines[:2]
            baseline = top + (33 if len(lines) == 1 else 24)
            for index, line in enumerate(lines):
                parts.append(f'<text x="{x + box_width/2}" y="{baseline + index*18}" text-anchor="middle" font-size="14" fill="#0f172a">{escape(line)}</text>')
        y += height + 14

    header = f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{y+8}" viewBox="0 0 {width} {y+8}"><defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="7" refY="4" orient="auto"><path d="M1,1 L7,4 L1,7" fill="none" stroke="#475569" stroke-width="1.5"/></marker></defs>'
    (results / 'graph.svg').write_text(header + '\n'.join(parts) + '</svg>', encoding='utf-8')


def main():
    nodes = read_table('nodes.csv')
    links = read_table('links.csv')
    scenarios = read_table('scenarios.csv')
    names = {node['id']: node['name'] for node in nodes}

    if len(names) != len(nodes):
        raise ValueError('В nodes.csv есть повторяющиеся ID')
    for link in links:
        if link['source'] not in names or link['target'] not in names:
            raise ValueError('В links.csv есть связь с неизвестным узлом')
    for scenario in scenarios:
        if scenario['root'] not in names:
            raise ValueError('В scenarios.csv указан неизвестный узел')

    print('МОДЕЛЬ ЗАВИСИМОСТЕЙ VK\n')
    rows = []
    for scenario in scenarios:
        p, i, d = (int(scenario[field]) for field in ('P', 'I', 'D'))
        if any(value < 1 or value > 5 for value in (p, i, d)):
            raise ValueError('Оценки P, I, D должны быть от 1 до 5')
        risk = round(p * i * d / 125 * 100, 1)
        affected = find_affected(scenario['root'], links)
        rows.append({'name': scenario['name'], 'risk': risk,
                     'affected': ', '.join(names[node] for node in affected[1:])})

    rows.sort(key=lambda row: row['risk'], reverse=True)
    for number, row in enumerate(rows, 1):
        print(f'{number}. {row["name"]}: {row["risk"]} балла')
        print(f'   Возможные последствия: {row["affected"]}')

    RESULTS.mkdir(exist_ok=True)
    with open(RESULTS / 'risks.csv', 'w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=['name', 'risk', 'affected'])
        writer.writeheader()
        writer.writerows(rows)

    draw_graph(nodes, links, scenarios, RESULTS)
    print('\nСохранено: results/risks.csv, results/graph.svg')


if __name__ == '__main__':
    main()
