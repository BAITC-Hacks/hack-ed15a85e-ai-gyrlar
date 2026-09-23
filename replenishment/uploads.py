"""Bounded, local Excel/CSV upload with preview and explicit column mapping."""
from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from .engine import clean_transactions

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 250_000
MAX_COLUMNS = 240
FIELDS = {
    'code': ('Код товара / артикул', ['код товара', 'код', 'sku', 'артикул', 'номенклатура.код', 'code', 'product id']),
    'date': ('Дата продажи', ['дата', 'период', 'дата продажи', 'date']),
    'qty': ('Количество продано', ['количество', 'продажи', 'продано', 'количество продано', 'qty', 'quantity', 'sales']),
    'name': ('Название товара', ['наименование', 'товар', 'номенклатура', 'название', 'название товара', 'name']),
    'supplier': ('Поставщик', ['поставщик', 'supplier']),
    'unit': ('Единица измерения', ['ед.', 'ед.изм', 'единица', 'единица измерения', 'unit']),
    'stock': ('Текущий свободный остаток', ['остаток', 'свободный остаток', 'текущий остаток', 'stock']),
    'stock_date': ('Дата остатка', ['дата остатка', 'stock date', 'stock_date']),
    'pack': ('Кратность заказа', ['кратность', 'упаковка', 'pack']),
    'moq': ('Минимальная партия', ['moq', 'минимальная партия', 'минимальный заказ']),
    'transit': ('Количество в пути', ['в пути', 'количество в пути', 'transit']),
    'eta': ('Дата поступления', ['дата поступления', 'eta']),
    'order_id': ('Номер накладной', ['номер накладной', 'документ', 'номер документа', 'order_id']),
}
MONTHS = {'янв': 1, 'фев': 2, 'мар': 3, 'апр': 4, 'май': 5, 'июн': 6,
          'июл': 7, 'авг': 8, 'сен': 9, 'окт': 10, 'ноя': 11, 'дек': 12}


def label(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value if value is not None else '').strip()[:500]


def month(value):
    text = label(value).lower()
    match = re.fullmatch(r'(20\d{2})[-/.](\d{1,2})(?:[-/.]\d{1,2})?', text)
    if match and 1 <= int(match[2]) <= 12:
        return f'{match[1]}-{int(match[2]):02}'
    match = re.fullmatch(r'(\d{1,2})[./](20\d{2})', text)
    if match and 1 <= int(match[1]) <= 12:
        return f'{match[2]}-{int(match[1]):02}'
    year = re.search(r'20\d{2}', text)
    return f'{year[0]}-{MONTHS[text[:3]]:02}' if year and text[:3] in MONTHS else None


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = label(value).split('T')[0].split(' ')[0]
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y'):
        try:
            result = datetime.strptime(text, fmt).date()
            if 2000 <= result.year <= 2100:
                return result
        except ValueError:
            pass
    raise ValueError('нужна дата в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД')


def numeric(value, optional=False):
    if value is None or label(value) == '':
        if optional:
            return None
        raise ValueError('пустое количество; укажите число, в том числе 0')
    if isinstance(value, bool):
        raise ValueError('нужно число')
    try:
        result = float(str(value).replace('\xa0', '').replace(' ', '').replace(',', '.'))
    except (ValueError, TypeError):
        raise ValueError('нужно число, например 12 или 12,5') from None
    if not math.isfinite(result) or abs(result) > 10**9:
        raise ValueError('число вне допустимого диапазона')
    return result


def inspect_file(content, filename):
    if not content or len(content) > MAX_BYTES:
        raise ValueError('Выберите непустой файл размером до 20 МБ.')
    filename = filename.replace('\\', '/').split('/')[-1][:160]
    suffix = Path(filename).suffix.lower()
    tables = []

    def collect(name, rows):
        rows = iter(rows)
        header = next(rows, None)
        if not header:
            return
        if len(header) > MAX_COLUMNS:
            raise ValueError('В таблице больше 240 столбцов. Оставьте только данные продаж.')
        columns = [label(v) or f'Столбец {i+1}' for i, v in enumerate(header)]
        data = []
        for line, row in enumerate(rows, 2):
            if line > MAX_ROWS+1:
                raise ValueError('Лимит — 250 000 строк на файл.')
            if not any(v is not None and str(v).strip() for v in row):
                continue
            if len(row) > len(columns) and any(label(v) for v in row[len(columns):]):
                raise ValueError(f'Строка {line}: больше значений, чем заголовков.')
            data.append((line, list(row[:len(columns)])+[None]*max(0, len(columns)-len(row))))
            if len(data)*len(columns) > 3_000_000:
                raise ValueError('Таблица слишком большая. Оставьте только столбцы, нужные для анализа.')
        if data:
            mapping = {key: next((i for i, col in enumerate(columns) if col.lower() in aliases), None)
                       for key, (_, aliases) in FIELDS.items()}
            monthly = {str(i): month(col) for i, col in enumerate(columns) if month(col)}
            tables.append(dict(name=name, columns=columns, data=data, mapping=mapping,
                               monthly=monthly, mode='monthly' if monthly else 'transactions'))

    if suffix == '.csv':
        for encoding in ('utf-8-sig', 'cp1251'):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
        else:
            raise ValueError('Сохраните CSV в кодировке UTF-8.')
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=';,\t')
        except csv.Error:
            raise ValueError('Не удалось разделить столбцы. Используйте CSV с «;» или запятой.') from None
        collect('Продажи', csv.reader(io.StringIO(text), dialect))
    elif suffix == '.xlsx':
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise ValueError('Для Excel установите зависимости: python -m pip install -r requirements.txt') from None
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if len(archive.infolist()) > 2000 or sum(i.file_size for i in archive.infolist()) > 100*1024*1024:
                    raise ValueError('Excel слишком большой после распаковки. Разделите таблицу.')
            book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            try:
                if len(book.worksheets) > 10:
                    raise ValueError('Оставьте в файле не более 10 листов.')
                for sheet in book.worksheets:
                    sheet.reset_dimensions()
                    collect(sheet.title, sheet.iter_rows(values_only=True))
                    if sum(len(t['data']) for t in tables) > MAX_ROWS:
                        raise ValueError('Лимит — 250 000 строк на файл.')
            finally:
                book.close()
        except ValueError:
            raise
        except Exception:
            raise ValueError('Не удалось прочитать Excel. Сохраните обычный .xlsx без пароля.') from None
    else:
        raise ValueError('Поддерживаются .xlsx и .csv. Старый .xls сохраните как .xlsx.')
    if not tables:
        raise ValueError('В файле нет строк данных. В первой строке нужны названия столбцов.')
    return dict(file=filename, bytes=len(content), sha256=hashlib.sha256(content).hexdigest(), tables=tables)


def preview(upload):
    return dict(file=upload['file'], fields=[dict(key=k, label=v[0]) for k, v in FIELDS.items()],
                sheets=[{**{k: t[k] for k in ('name', 'columns', 'mapping', 'monthly', 'mode')},
                         'rows': len(t['data']), 'sample': [[label(v) for v in r] for _, r in t['data'][:4]]}
                        for t in upload['tables']], as_of=date.today().isoformat())


def build_dataset(upload, options):
    index = options.get('sheet')
    if type(index) is not int or not 0 <= index < len(upload['tables']):
        raise ValueError('Выберите лист с продажами.')
    table = upload['tables'][index]
    mapping = options.get('mapping')
    if not isinstance(mapping, dict) or set(mapping)-set(FIELDS):
        raise ValueError('Проверьте соответствие столбцов.')
    for value in mapping.values():
        if value is not None and (type(value) is not int or not 0 <= value < len(table['columns'])):
            raise ValueError('Выбран неизвестный столбец.')
    used = [v for v in mapping.values() if v is not None]
    if len(used) != len(set(used)):
        raise ValueError('Один столбец нельзя назначить нескольким полям.')
    mode = options.get('mode')
    if mode not in ('monthly', 'transactions'):
        raise ValueError('Выберите формат продаж.')
    required = ['code'] if mode == 'monthly' else ['code', 'date', 'qty']
    for key in required:
        if mapping.get(key) is None:
            raise ValueError('Укажите столбец «'+FIELDS[key][0]+'».')
    as_of = parse_date(options.get('as_of'))
    monthly = table['monthly']
    if mode == 'monthly' and not monthly:
        raise ValueError('Нет столбцов месяцев. Используйте заголовки 2026-01 или Январь 2026.')
    if mode == 'monthly' and (len(set(monthly.values())) != len(monthly) or set(map(int, monthly)) & set(used)):
        raise ValueError('Месяцы повторяются или назначены другим полям.')
    items, events = {}, defaultdict(list)
    diagnostics = Counter()
    seen_wide = set()
    dates = []
    for line, row in table['data']:
        def cell(key):
            col = mapping.get(key)
            return row[col] if col is not None else None
        try:
            code = label(cell('code'))
            if not code and label(cell('name')).lower() in ('итого', 'всего', 'total', 'общий итог'):
                diagnostics['skipped_total_rows'] += 1
                continue
            if not code and not label(cell('name')) and mode == 'monthly' and line <= 3:
                month_labels = [label(row[int(col)]).lower() for col in monthly]
                if any(month_labels) and all(v in ('', 'количество', 'quantity') for v in month_labels):
                    diagnostics['skipped_header_rows'] += 1
                    continue
            if not code:
                raise ValueError('пустой код товара; уберите итоговые строки')
            if code.lower() in ('итого', 'всего', 'total', 'общий итог'):
                raise ValueError('удалите строку итогов из таблицы')
            supplier = label(cell('supplier')) or 'Мой поставщик'
            # Hash the pair so delimiters within a supplier/code cannot merge goods.
            key = 'upload:'+hashlib.sha256((supplier+'\0'+code).encode()).hexdigest()[:24]
            if key not in items:
                items[key] = dict(id=key, code=code, article=code, name=label(cell('name')) or code,
                                  supplier=supplier, unit=label(cell('unit')), category='Без категории', sales={},
                                  stock=None, stock_date=None, pack=None, moq=None, transit=[], warnings=[],
                                  sources=[f'{upload["file"]} · {table["name"]} · код {code}'])
            obj = items[key]
            if len(items) > 10_000:
                raise ValueError('лимит — 10 000 товаров')
            if label(cell('unit')) and obj['unit'] and label(cell('unit')) != obj['unit']:
                raise ValueError(f'для {code} указаны разные единицы измерения')
            for field in ('stock', 'pack', 'moq', 'transit'):
                value = numeric(cell(field), optional=True)
                if value is not None:
                    if field == 'pack' and value == 0:
                        obj['warnings'].append('Кратность 0 в файле принята за неизвестную. Уточните её перед заказом.')
                        continue
                    if value < 0:
                        raise ValueError(f'«{FIELDS[field][0]}»: недопустимое отрицательное или нулевое значение')
                    store = '_transit' if field == 'transit' else field
                    if obj.get(store) is not None and obj[store] != value:
                        raise ValueError(f'у {code} разные значения «{FIELDS[field][0]}». Нужен один актуальный снимок, не остаток после каждой продажи')
                    obj[store] = value
            for field in ('stock_date', 'eta'):
                if label(cell(field)):
                    value = parse_date(cell(field)).isoformat()
                    if obj.get(field) and obj[field] != value:
                        raise ValueError(f'у {code} разные даты «{FIELDS[field][0]}»')
                    obj[field] = value
            if mode == 'transactions':
                d = parse_date(cell('date'))
                if d > as_of:
                    raise ValueError('продажа позже даты расчёта')
                qty = numeric(cell('qty'))
                m = d.strftime('%Y-%m')
                dates.append(m)
                obj['sales'][m] = obj['sales'].get(m, 0)+qty
                if qty > 0:
                    events[key].append(dict(date=d.isoformat(), qty=qty, order_id=label(cell('order_id')) or str(line)))
                diagnostics['transaction_rows'] += 1
                diagnostics['negative_adjustments'] += qty < 0
            else:
                if key in seen_wide:
                    raise ValueError(f'товар {code} повторяется; оставьте одну строку на поставщика и код')
                seen_wide.add(key)
                for col, m in monthly.items():
                    qty = numeric(row[int(col)], optional=True)
                    obj['sales'][m] = qty if qty is not None else 0
                    diagnostics['blank_months'] += qty is None
                    diagnostics['negative_adjustments'] += (qty or 0) < 0
                    dates.append(m)
        except ValueError as exc:
            raise ValueError(f'Строка {line}: {exc}.') from None
    complete = sorted({m for m in dates if m < as_of.strftime('%Y-%m')})
    if not complete:
        raise ValueError('Нет продаж за завершённые месяцы до даты расчёта. Добавьте историю или измените дату.')
    start, end = min(dates), max(dates)
    if int(end[:4])-int(start[:4]) > 10:
        raise ValueError('История длиннее 10 лет. Оставьте последние годы.')
    limitations = [
        'Анализируется выбранный лист. Другие листы и прошлые загрузки с ним не суммируются.',
        'Прогноз использует только завершённые месяцы до даты расчёта. Текущий и будущие месяцы исключены.',
        'Пропуски месяцев внутри периода файла приняты за отсутствие продаж. Загружайте полную историю, желательно за 12–24 месяца.',
        'Сезонные коэффициенты и периоды отсутствия товара не загружены: сезонность нейтральная, потерянный спрос не восстанавливается.',
        'Срок поставки 30 дней, цикл 14 дней и запас 7 дней — начальные допущения. Измените их в параметрах.',
        'Без актуального остатка количество заказа не рассчитывается. Без кратности заказ предварительный.',
    ]
    if mode == 'monthly':
        limitations.append('При месячной истории доступны только месячные всплески; отдельные крупные накладные определить нельзя. Пустые месячные ячейки приняты за 0.')
    if mode == 'transactions' and mapping.get('order_id') is None:
        limitations.append('Номер накладной не выбран: каждая строка считается отдельной продажей при проверке всплесков.')
    for obj in items.values():
        # Shared report coverage preserves trailing zero months without inventing time after the file.
        year, mon = map(int, start.split('-'))
        while f'{year:04}-{mon:02}' <= end:
            obj['sales'].setdefault(f'{year:04}-{mon:02}', 0)
            year, mon = (year+1, 1) if mon == 12 else (year, mon+1)
        if obj['stock'] is not None and not obj['stock_date']:
            obj['stock_date'] = as_of.isoformat()
            obj['warnings'].append('Дата остатка не указана: принята выбранная дата расчёта. Подтвердите актуальность.')
        if obj['stock_date'] and obj['stock_date'] > as_of.isoformat():
            raise ValueError(f'{obj["code"]}: дата остатка позже даты расчёта.')
        if len(complete) < 6:
            obj['warnings'].append('Менее 6 полных месяцев истории: короткая база прогноза.')
        if complete[-1] < f'{as_of.year if as_of.month > 1 else as_of.year-1:04}-{as_of.month-1 if as_of.month > 1 else 12:02}':
            obj['warnings'].append('История заканчивается раньше последнего полного месяца. Проверьте актуальность выгрузки.')
        transit = obj.pop('_transit', 0)
        if transit:
            obj['transit'] = [dict(qty=transit, eta=obj.get('eta'), source=upload['file'])]
        obj['outlier_removed'] = defaultdict(float)
        obj['anomalies'] = []
        for event in clean_transactions(events[obj['id']]):
            if event['qty'] > event['clean']:
                obj['outlier_removed'][event['date'][:7]] += event['qty']-event['clean']
                obj['anomalies'].append(dict(date=event['date'], qty=event['qty'], regular=event['clean'], reason=event['reason']))
        diagnostics['detected_document_outliers'] += len(obj['anomalies'])
    diagnostics['imported_rows'] = len(table['data'])-diagnostics['skipped_total_rows']-diagnostics['skipped_header_rows']
    skipped = diagnostics['skipped_total_rows']+diagnostics['skipped_header_rows']
    if skipped:
        limitations.insert(0, f'Пропущено служебных строк: {skipped} (повторная шапка «Количество» или явно подписанный итог без кода).')
    return dict(version=1, as_of=as_of.isoformat(), items=list(items.values()),
                seasonality={i['supplier']: [1.0]*12 for i in items.values()},
                sources=[dict(file=upload['file'], sha256=upload['sha256'], bytes=upload['bytes'], supplier='Пользовательский файл')],
                diagnostics=dict(diagnostics), limitations=limitations,
                upload=dict(file=upload['file'], sheet=table['name'], format=mode, period=[complete[0], complete[-1]]))
