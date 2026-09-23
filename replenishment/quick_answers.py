"""Short, calculated answers to narrowly defined UI questions.

Free-form questions deliberately fall through to the language model. Quantities
come from the current calculation; no network or persistent state is used here.
"""
import math
import re


URGENT_QUESTIONS = {'какие позиции требуют срочного заказа', 'что заказать в первую очередь'}
ANOMALY_QUESTIONS = {'покажи разовые всплески продаж', 'где обнаружены всплески'}
GROWTH_PATTERN = re.compile(
    r'(?:сценарий роста(?: спроса)?|что изменится при росте спроса на)\s+'
    r'([+-]?\d+(?:[.,]\d+)?)\s*%')


def _normalise(text):
    return ' '.join(text.strip().casefold().replace('ё', 'е').split()).rstrip('?.!')


def _number(value):
    return f'{value:,.3f}'.rstrip('0').rstrip('.').replace(',', ' ')


def _text(value, limit=90):
    return ' '.join(str(value or '').split())[:limit]


def _label(row):
    return f'{_text(row.get("article") or row.get("code"), 60)} ({_text(row.get("supplier"), 45)})'


def _quantity(row):
    if row.get('stock') is None or row.get('qty') is None:
        return None
    # Match State.create_orders: approved row edits replace the recommendation;
    # reservations are deducted before applying the supplier's constraints.
    wanted = row['review']['qty'] if row.get('review') else row['qty']
    value = max(0, wanted-row.get('draft_qty', 0))
    if value <= 0:
        return 0
    pack = row.get('pack') or 1
    return math.ceil(max(value, row.get('moq') or 0)/pack-1e-10)*pack


def _unit(row):
    return _text(row.get('unit')) or 'ед. (единица не указана)'


def _warnings(row):
    """Keep actionable source warnings, including the actual stock date."""
    priorities = ('остат', 'Дефицит', 'Просроч', 'без даты', 'кратност', 'истории')
    warnings = row.get('warnings', [])
    selected = []
    for word in priorities:
        match = next((w for w in warnings if word.casefold() in w.casefold() and w not in selected), None)
        if match:
            selected.append(match)
    return '; '.join(_text(w, 160) for w in selected[:3])


def _urgent(rows):
    urgent = [r for r in rows if r.get('urgency') == 'Срочно']
    urgent.sort(key=lambda r: (r.get('shortage_days') if r.get('shortage_days') is not None else math.inf,
                               str(r.get('supplier', '')), str(r.get('code', ''))))
    missing = sum(r.get('stock') is None for r in rows)
    if not urgent:
        answer = 'По текущему расчёту срочных позиций нет.'
        if missing:
            answer += f' Для {missing} позиций остаток неизвестен: их риск пока нельзя оценить.'
        return answer+'\nСледующий шаг: проверьте актуальность остатков и плановые рекомендации в анализе.'
    lines = [f'Риск дефицита до новой поставки: {len(urgent)} позиций. Первые по сроку дефицита:']
    for row in urgent[:5]:
        qty = _quantity(row)
        unit = _unit(row)
        if qty is None:
            action = 'количество не рассчитано: нужен остаток'
        elif qty > 0:
            action = f'новый черновик: {_number(qty)} {unit}'
        elif row.get('draft_qty', 0) > 0:
            action = f'уже в черновиках: {_number(row["draft_qty"])} {unit}; повторный заказ не нужен'
        else:
            action = 'дополнительный заказ не нужен; ускорьте поступление'
        if row.get('status') == 'Предварительно':
            action += f'; предварительно, остаток на {_text(row.get("stock_date")) or "неизвестную дату"}'
        lines.append(f'• {_label(row)} — {action}.')
    if missing:
        lines.append(f'Ещё {missing} позиций без остатка — количество не рассчитано.')
    lines.append('Следующий шаг: сверьте остатки и согласуйте ускорение поставки или перемещение — новый заказ не закрывает дефицит до прибытия.')
    return '\n'.join(lines)


def _anomalies(rows):
    candidates = [r for r in rows if r.get('removed', 0) > 0 or any(
        h.get('candidate_removed', 0) > 0 for h in r.get('history', []))]
    if not candidates:
        return 'В текущей истории кандидаты на разовые всплески не обнаружены.\nСледующий шаг: проверьте полноту загруженных продаж.'
    candidates.sort(key=lambda r: (str(r.get('supplier', '')), str(r.get('code', ''))))
    lines = [f'Кандидаты на разовые всплески: {len(candidates)} позиций. Примеры:']
    for row in candidates[:5]:
        removed = row.get('removed', 0)
        detail = (f'из расчёта последних 12 полных месяцев исключено {_number(removed)} {_unit(row)}'
                  if removed else 'кандидат найден; продажи сохранены в расчёте')
        lines.append(f'• {_label(row)} — {detail}.')
    lines.append('Это статистические кандидаты, а не подтверждённые проекты.\nСледующий шаг: откройте «Проверка всплесков» и подтвердите обычный спрос или разовую продажу.')
    return '\n'.join(lines)


def _explain(row):
    qty = _quantity(row)
    unit = _unit(row)
    lines = [_label(row)+':']
    if qty is None:
        lines.append('Остаток неизвестен, поэтому количество заказа не рассчитано.')
    else:
        lines.append(f'Можно включить в новый черновик: {_number(qty)} {unit}.')
        if row.get('draft_qty', 0):
            lines.append(f'В сохранённых черновиках уже {_number(row["draft_qty"])} {unit}; повторно их не заказывайте.')
        lines.append(f'Спрос на {_number(row["lead_days"]+row["review_days"])} дн.: {_number(row["demand"])}; '
                     f'страховой запас: {_number(row["safety"])}; остаток: {_number(row["stock"])}; '
                     f'поставки в этот период: {_number(row["in_transit"])} {unit}.')
        if row.get('review') and row['review']['qty'] != row['qty']:
            lines.append(f'Учтена правка менеджера: {_number(row["review"]["qty"])} {unit} до вычета черновиков.')
        if row.get('pack'):
            minimum = f'; минимум заказа {_number(row["moq"])} {unit}' if row.get('moq', 0) > 0 else ''
            lines.append(f'Кратность: {_number(row["pack"])} {unit}{minimum}.')
    warnings = _warnings(row)
    if warnings:
        lines.append('Проверить: '+warnings+'.')
    if qty is None or row.get('status') == 'Предварительно':
        lines.append('Следующий шаг: подтвердите актуальный остаток в карточке товара.')
    elif row.get('urgency') == 'Срочно':
        lines.append('Следующий шаг: согласуйте ускорение поставки или перемещение до наступления дефицита.')
    elif qty > 0:
        lines.append('Следующий шаг: проверьте количество и создайте черновик заказа.')
    else:
        lines.append('Следующий шаг: проверьте даты ожидаемых поставок и сохранённые черновики.')
    return '\n'.join(lines)


def _scenario(value, rows, run_tool):
    if not math.isfinite(value) or not -90 <= value <= 300:
        return 'Для сценария укажите изменение спроса от −90% до 300%. Текущий расчёт не изменён.'
    result = run_tool('simulate_scenario', {'parameter': 'growth_pct', 'value': value})
    if result.get('error'):
        return 'Сценарий не рассчитан: '+str(result['error'])
    lines = [f'Сценарий изменения спроса: {value:+g}%. Позиций с потребностью в заказе: '
             f'{result["before"]["to_order"]} → {result["after"]["to_order"]}. '
             f'Количество изменилось у {result["changed_count"]} позиций.']
    for row in result.get('examples', [])[:3]:
        before, after = row.get('before'), row.get('after')
        if before is not None and after is not None:
            delta = after-before
            lines.append(f'• {_text(row.get("article") or row.get("id"))}: {_number(before)} → {_number(after)} '
                         f'{_unit(row)} ({"+" if delta >= 0 else ""}{_number(delta)}).')
    missing = result['before'].get('missing_stock', 0)
    preliminary = result['before'].get('preliminary', 0)
    if missing:
        lines.append(f'Для {missing} позиций остаток неизвестен: количество не рассчитано.')
    if preliminary:
        lines.append(f'У {preliminary} позиций расчёт предварительный: сверьте даты остатков и кратность.')
    if any(r.get('draft_qty', 0) for r in rows):
        lines.append('Показана расчётная потребность до вычета сохранённых черновиков.')
    lines.append('Параметры не сохранены.\nСледующий шаг: сравните риск дефицита и подтвердите нужный сценарий в настройках.')
    return '\n'.join(lines)


def quick_answer(message, dataset, settings, rows, run_tool, context=None):
    """Return an answer only for explicit, standalone, supported questions."""
    text = _normalise(message)
    if text in URGENT_QUESTIONS:
        run_tool('inventory_summary', {})
        return {'answer': _urgent(rows)}
    if text in ANOMALY_QUESTIONS:
        run_tool('inventory_summary', {})
        return {'answer': _anomalies(rows)}
    growth = GROWTH_PATTERN.fullmatch(text)
    if growth:
        return {'answer': _scenario(float(growth.group(1).replace(',', '.')), rows, run_tool)}
    prefix = 'объясни расчет для '
    if not text.startswith(prefix):
        return None
    identifier = text[len(prefix):]
    found = [r for r in rows if any(identifier == _normalise(str(r.get(key, '')))
                                    for key in ('article', 'code') if r.get(key))]
    if not found:
        return None
    if isinstance(context, dict) and context.get('item_id'):
        contextual = [r for r in found if r['id'] == context['item_id']]
        if contextual:
            found = contextual
    if len(found) > 1:
        suppliers = ', '.join(dict.fromkeys(_text(r.get('supplier')) for r in found[:5]))
        return {'answer': f'Артикул или код {_text(identifier)} соответствует {len(found)} товарам: {suppliers}. '
                'Откройте нужную карточку товара и нажмите «Объяснить с агентом», чтобы выбрать точный товар.'}
    row = found[0]
    run_tool('explain_product', {'id': row['id']})
    return {'answer': _explain(row)}
