"""Local folder import helpers; scheduling and persistence belong to the caller.

Recognized files are sales.csv|xlsx, stock.csv|xlsx and transit.csv|xlsx.
Each source must use one nonempty sheet with recognizable column headers.
Helpers never update application state, files, hash dictionaries or datasets.
"""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from .uploads import MAX_BYTES, inspect_file, parse_date, preview

SOURCE_KINDS = ('sales', 'stock', 'transit')
CONFIG_FIELDS = ('enabled', 'interval_minutes', 'advance_date', 'folder')
DEFAULT_CONFIG = dict(enabled=False, interval_minutes=60, advance_date=True, folder='')


def validate_config(request, current=None):
    """Return validated settings only, preserving omitted fields from current.

    A blank folder enables recalculation of existing data without any file I/O.
    A nonblank folder is absolute. When enabled it must already be a directory;
    while disabled a future absolute directory may be saved for later setup.
    Runtime fields (last_run, error, hashes, etc.) remain owned by the caller.
    """
    if not isinstance(request, dict):
        raise ValueError('Настройки автоматизации должны быть объектом.')
    if set(request)-set(CONFIG_FIELDS):
        raise ValueError('Неизвестные параметры автоматизации: '+', '.join(sorted(set(request)-set(CONFIG_FIELDS)))+'.')
    if current is not None and not isinstance(current, dict):
        raise ValueError('Сохранённые настройки автоматизации повреждены.')
    result = {key: (current or {}).get(key, DEFAULT_CONFIG[key]) for key in CONFIG_FIELDS}
    result.update(request)
    for key, title in (('enabled', 'Включение'), ('advance_date', 'Обновление даты расчёта')):
        if type(result[key]) is not bool:
            raise ValueError(f'{title}: выберите да или нет.')
    interval = result['interval_minutes']
    if type(interval) is not int or not 1 <= interval <= 10080:
        raise ValueError('Интервал пересчёта — целое число от 1 до 10080 минут (7 дней).')
    if not isinstance(result['folder'], str):
        raise ValueError('Папка выгрузок должна быть строкой с абсолютным путём.')
    result['folder'] = result['folder'].strip()
    if result['folder']:
        path = Path(result['folder'])
        if not path.is_absolute():
            raise ValueError('Укажите абсолютный путь к папке выгрузок, например C:\\Data\\StockPilot.')
        if result['enabled']:
            try:
                if not path.is_dir():
                    raise ValueError('Папка выгрузок не найдена. Создайте её или очистите путь для пересчёта текущих данных.')
                result['folder'] = str(path.resolve(strict=True))
            except OSError:
                raise ValueError('Не удалось открыть папку выгрузок. Проверьте путь и права доступа.') from None
        else:
            result['folder'] = str(path)
    return result


def collect_sources(folder, hashes=None, *, as_of=None, supplier=''):
    """Read and preview changed source files, atomically returning two dicts.

    Returns (changed, all_hashes):
      changed[kind] = {upload, options, hash}
      all_hashes[kind] = sha256 for each currently present recognized file.

    options has sheet, mode, mapping, as_of and optional supplier, ready for
    build_dataset / merge_source. Filenames are matched case-insensitively;
    unknown files are ignored. A missing source is not a deletion request.
    The caller must validate and apply every changed source to a working copy,
    then persist its data and all_hashes together only after complete success.
    """
    if not isinstance(folder, str):
        raise ValueError('Папка выгрузок должна быть строкой с абсолютным путём.')
    folder = folder.strip()
    if not folder:
        return {}, {}
    if hashes is not None and not isinstance(hashes, dict):
        raise ValueError('Хеши предыдущих загрузок должны быть объектом.')
    hashes = hashes or {}
    if not isinstance(supplier, str):
        raise ValueError('Название поставщика должно быть строкой.')
    when = parse_date(as_of) if as_of is not None else date.today()
    path = Path(folder)
    if not path.is_absolute():
        raise ValueError('Для автоматической загрузки нужен абсолютный путь к папке.')
    try:
        if not path.is_dir():
            raise ValueError('Папка автоматической загрузки не найдена.')
        entries = list(path.iterdir())
    except OSError:
        raise ValueError('Не удалось прочитать папку автоматической загрузки. Проверьте права доступа.') from None

    candidates = {kind: [] for kind in SOURCE_KINDS}
    for entry in entries:
        name = entry.name.casefold()
        for kind in SOURCE_KINDS:
            if name in (kind+'.csv', kind+'.xlsx'):
                candidates[kind].append(entry)
                break
    changed, all_hashes, errors = {}, {}, []
    for kind in SOURCE_KINDS:
        files = candidates[kind]
        if len(files) > 1:
            errors.append(f'{kind}: найдены несколько файлов ({", ".join(sorted(item.name for item in files))}). Оставьте один .csv или .xlsx для этого источника.')
            continue
        if not files:
            continue
        source = files[0]
        try:
            if not source.is_file():
                raise ValueError('ожидался файл, а не папка')
            if source.stat().st_size > MAX_BYTES:
                raise ValueError('файл больше 20 МБ')
            with source.open('rb') as handle:
                content = handle.read(MAX_BYTES+1)
            if len(content) > MAX_BYTES:
                raise ValueError('файл больше 20 МБ')
            fingerprint = hashlib.sha256(content).hexdigest()
            all_hashes[kind] = fingerprint
            if fingerprint == hashes.get(kind):
                continue
            upload = inspect_file(content, source.name)
            info = preview(upload, kind)
            if len(info['sheets']) != 1:
                raise ValueError('для автоматической загрузки нужен ровно один непустой лист. Выберите нужный лист и сохраните его отдельным файлом')
            sheet = info['sheets'][0]
            required = ['code'] if kind == 'sales' and sheet['mode'] == 'monthly' else info['required']
            labels = {field['key']: field['label'] for field in info['fields']}
            missing = [labels[key] for key in required if sheet['mapping'].get(key) is None]
            if missing:
                raise ValueError('не распознаны столбцы: '+', '.join(missing)+'. Используйте заголовки из шаблона или сначала загрузите файл вручную')
            options = dict(sheet=0, mode=sheet['mode'], mapping=dict(sheet['mapping']), as_of=when.isoformat())
            if supplier.strip():
                options['supplier'] = supplier.strip()
            changed[kind] = dict(upload=upload, options=options, hash=fingerprint)
        except (OSError, ValueError) as exc:
            detail = str(exc) if isinstance(exc, ValueError) else 'не удалось прочитать файл; проверьте права доступа и завершите его сохранение'
            errors.append(f'{source.name}: {detail}.')
    if errors:
        raise ValueError('Автоматическая загрузка отменена. '+' '.join(errors))
    return changed, all_hashes
