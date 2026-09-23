"""Deterministic, read-only anomaly review and chronological forecast validation."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from .engine import build_history, calculate, forecast_level, month_range, number


def anomaly_report(dataset, settings, rows, limit=500, offset=0):
    """Review auto-detected months with isolated keep/clean order scenarios.

    Scenario quantities use the active stock, transit and all OTHER decisions.
    They are not additive: two month changes can interact through trend/rounding.
    """
    catalog = {item['id']: item for item in dataset['items']}
    candidates = []
    for row in rows:
        for history in row.get('history', []):
            if history.get('candidate_removed', 0) <= 0:
                continue
            candidates.append((row, history))
    candidates.sort(key=lambda pair: (
        pair[1]['month'], pair[1]['candidate_removed']/max(pair[1]['raw'], 1e-9),
        pair[0]['supplier'], pair[0]['id']), reverse=True)
    limit = max(1, min(1000, int(limit)))
    offset = max(0, int(offset))
    items = []
    for row, history in candidates[offset:offset+limit]:
        item = catalog[row['id']]
        month = history['month']
        orders = {}
        for choice in ('keep', 'exclude'):
            scenario = dict(item, anomaly_decisions=dict(item.get('anomaly_decisions', {}), **{month: choice}))
            orders[choice] = calculate(scenario, dataset['seasonality'][item['supplier']], settings)['qty']
        items.append(dict(
            id=row['id'], code=row['code'], article=row.get('article', ''),
            name=row.get('name', ''), supplier=row['supplier'], unit=row.get('unit', ''),
            month=month, raw=history['raw'], regular=history['candidate_regular'],
            removed=history['candidate_removed'], decision=history.get('decision', 'auto'),
            reason=history.get('anomaly_reason', 'Статистически обнаруженный всплеск'),
            order_before=orders['keep'], order_after=orders['exclude'], current_order=row['qty']))
    decisions = [history.get('decision', 'auto') for _, history in candidates]
    return dict(items=items, limit=limit, offset=offset, summary=dict(
        candidate_months=len(candidates), products=len({row['id'] for row, _ in candidates}),
        pending=decisions.count('auto'), kept=decisions.count('keep'), excluded=decisions.count('exclude'),
        reported=len(items), truncated=offset+len(items) < len(candidates)),
        explanation='До — заказ, если оставить исходные продажи выбранного месяца; после — если использовать регулярную оценку. Остальные решения сохранены. Разницы между строками нельзя складывать.')


def _metrics(samples, unit):
    actual = sum(sample['actual'] for sample in samples)
    metrics = dict(unit=unit, samples=len(samples), actual_total=round(actual, 4))
    for name, field in (('model', 'predicted'), ('baseline', 'baseline')):
        absolute_error = sum(abs(sample[field]-sample['actual']) for sample in samples)
        metrics[name] = dict(
            mae=round(absolute_error/len(samples), 4),
            wape=round(100*absolute_error/actual, 4) if actual > 0 else None,
            predicted_total=round(sum(sample[field] for sample in samples), 4))
    metrics['wape_improvement_pp'] = round(metrics['baseline']['wape']-metrics['model']['wape'], 4) if actual > 0 else None
    return metrics


def backtest(dataset, settings, months=3):
    """Rolling one-month holdouts; the held-out month never enters its model.

    Validate demand in native units, not order quantities. Restrict inputs to raw
    completed sales: precomputed transaction cleaning, season indices, stockout
    imputations and manual decisions may contain future knowledge and are not
    appropriate for a retrospective score.
    """
    as_of = date.fromisoformat(settings.get('as_of', dataset.get('as_of', '2026-09-22')))
    evaluation_months = month_range(as_of, max(1, min(3, int(months))))
    samples = []
    skipped = dict(missing_actual=0, insufficient_history=0)
    for item in dataset['items']:
        sales = item.get('sales', {})
        for month in evaluation_months:
            if month not in sales:
                skipped['missing_actual'] += 1
                continue
            cutoff = date.fromisoformat(month+'-01')
            # An absent column is unknown, not evidence of zero demand.
            if any(prior not in sales for prior in month_range(cutoff, 6)):
                skipped['insufficient_history'] += 1
                continue
            training_sales = {m: max(0, number(v)) for m, v in sales.items() if m < month}
            training = dict(sales=training_sales)
            history = build_history(training, [1.0]*12, cutoff,
                                    remove_outliers=settings.get('remove_outliers', True), compensate=False)
            base, growth, _ = forecast_level(history, [1.0]*12, settings.get('trend', True))
            trailing = [training_sales[m] for m in sorted(training_sales)[-12:]]
            predicted = max(0, base*(1+growth))
            actual = max(0, number(sales[month]))
            samples.append(dict(id=item['id'], code=item.get('code', ''), name=item.get('name', ''),
                                supplier=item.get('supplier', ''), unit=item.get('unit') or 'не указана',
                                month=month, actual=actual, predicted=predicted,
                                baseline=sum(trailing)/len(trailing), absolute_error=abs(predicted-actual)))
    by_unit = defaultdict(list)
    by_month = defaultdict(list)
    for sample in samples:
        by_unit[sample['unit']].append(sample)
        by_month[(sample['month'], sample['unit'])].append(sample)
    per_unit = [_metrics(group, unit) for unit, group in sorted(by_unit.items())]
    monthly = [dict(month=month, **_metrics(group, unit)) for (month, unit), group in sorted(by_month.items())]
    examples = []
    for unit, group in sorted(by_unit.items()):
        # Rank only within the same unit; metres and pieces are not comparable.
        for sample in sorted(group, key=lambda s: (-s['absolute_error'], s['id'], s['month']))[:5]:
            examples.append({key: round(value, 4) if isinstance(value, float) else value for key, value in sample.items()})
    return dict(
        status='ok' if samples else 'unavailable', evaluation_months=evaluation_months,
        min_training_months=6, samples=len(samples), products=len({sample['id'] for sample in samples}),
        total_pairs=len(dataset['items'])*len(evaluation_months), per_unit=per_unit,
        by_month=monthly, examples=examples, skipped=skipped,
        methodology=[
            'Скользящая проверка на последних трёх завершённых месяцах (или на запрошенном меньшем числе). Для каждого месяца используются только более ранние продажи.',
            'Допускаются товары с данными за шесть последовательных месяцев перед проверяемым. Отсутствующая колонка не заменяется нулём.',
            'Модель: взвешенное среднее до 12 месяцев, месячная очистка всплесков и ограниченный тренд согласно текущим переключателям. Очистка каждого учебного месяца использует только предшествующие ему месяцы.',
            'База сравнения: простое среднее исходных продаж до 12 предшествующих месяцев. Обе оценки сравниваются с одним и тем же исходным фактом проверяемого месяца.',
            'MAE — средняя абсолютная ошибка в единице товара. WAPE — сумма абсолютных ошибок / сумма фактических продаж × 100%. Ниже — лучше. При нулевом факте WAPE не определён.'
        ], limitations=[
            'Проверяются наблюдаемые продажи, а не скрытый спрос: отсутствие товара может снизить продажи. Разовые проекты в проверяемом факте сохраняются и могут ухудшать оценку регулярного спроса.',
            'Во избежание информации из будущего отключены готовые коэффициенты сезонности, очистка накладных по всей истории, ручные решения о всплесках, компенсация отсутствия товара и бизнес-прирост.',
            'Остатки, поставки, цены и сроки поставщиков не используются. Результат оценивает прогноз месячных продаж, а не точность заказов и не экономию денег.',
            'Метрики группируются по исходным единицам измерения без пересчёта упаковок. Неполные выгрузки и короткая история ограничивают выводы; пропущенные пары показаны отдельно.'
        ])
