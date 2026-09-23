"""Pure, deterministic demand and order calculations. No network or side effects."""
from __future__ import annotations

import calendar
import math
import statistics as st
from collections import defaultdict
from datetime import date, timedelta


def median(values):
    return st.median(values) if values else 0.0


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def clean_transactions(events):
    """Aggregate split lines per document, then detect customer-day concentration.

    Input: positive sale quantities; returns must be handled separately.
    A document number is never treated as a customer ID.
    """
    grouped = defaultdict(float)
    for e in events:
        if number(e.get('qty')) > 0:
            grouped[(e['date'][:10], e.get('order_id', ''), e.get('customer_id') or '')] += number(e['qty'])
    rows = [dict(date=k[0], order_id=k[1], customer_id=k[2], qty=v, clean=v) for k, v in grouped.items()]
    positive = [e['qty'] for e in rows]
    med = median(positive)
    mad = median([abs(v-med) for v in positive])
    threshold = max(6*med, med+8*1.4826*mad)
    if len(rows) >= 8 and med > 0:
        for e in rows:
            if e['qty'] > threshold:
                e['clean'] = med
                e['reason'] = 'Разовая крупная накладная'
    customer_days = defaultdict(list)
    for e in rows:
        if e['customer_id']:
            customer_days[(e['customer_id'], e['date'])].append(e)
    totals = [sum(e['clean'] for e in group) for group in customer_days.values()]
    center = median(totals)
    spread = median([abs(v-center) for v in totals])
    limit = max(6*center, center+8*1.4826*spread)
    if len(totals) >= 8 and center > 0:
        for group in customer_days.values():
            total = sum(e['clean'] for e in group)
            if total > limit:
                for e in group:
                    e['clean'] *= center/total
                    e['reason'] = 'Разовая концентрация у клиента за день'
    return rows


def month_days(month):
    y, m = map(int, month.split('-'))
    return calendar.monthrange(y, m)[1]


def month_range(end, count):
    index = end.year*12 + end.month-1
    return [f'{(index-i)//12:04d}-{(index-i)%12+1:02d}' for i in range(count, 0, -1)]


def build_history(item, season, as_of, remove_outliers=True, compensate=True):
    months = [m for m in sorted(item.get('sales', {})) if m < as_of.strftime('%Y-%m')][-24:]
    cleaned = []
    detected_removed = []
    reasons = []
    for m in months:
        raw = max(0, number(item['sales'][m]))
        # Detection is independent of the manager's choice and global switch.
        # Keeping a detected month must remain reversible in the review screen.
        removed = min(raw, max(0, number(item.get('outlier_removed', {}).get(m))))
        cleaned.append(raw-removed)
        detected_removed.append(removed)
        reasons.append(['Разовые крупные накладные или концентрация продаж у клиента'] if removed > 0 else [])
    # A second, deliberately conservative guard for monthly-only history.
    candidate = list(cleaned)
    for i, m in enumerate(months):
        recent = [cleaned[j]/season[int(months[j][5:])-1] for j in range(max(0, i-12), i)]
        positive = [v for v in recent if v > 0]
        if len(positive) >= 6:
            center = median(positive)
            mad = median([abs(v-center) for v in positive])
            value = cleaned[i]/season[int(m[5:])-1]
            if value > max(6*center, center+8*1.4826*mad):
                candidate[i] = center*season[int(m[5:])-1]
                detected_removed[i] += cleaned[i]-candidate[i]
                reasons[i].append('Месячный всплеск относительно предыдущих 12 месяцев')
    adjusted, excluded, decisions = [], [], []
    for i, m in enumerate(months):
        raw = max(0, number(item['sales'][m]))
        decision = item.get('anomaly_decisions', {}).get(m, 'auto')
        decision = decision if decision in ('auto', 'keep', 'exclude') else 'auto'
        apply_cleaning = decision == 'exclude' or (decision == 'auto' and remove_outliers)
        adjusted.append(candidate[i] if apply_cleaning else raw)
        excluded.append(detected_removed[i] if apply_cleaning else 0)
        decisions.append(decision)
    available_rates = []
    for i, m in enumerate(months):
        missing = number(item.get('stockout_days', {}).get(m))
        if missing == 0:
            available_rates.append(adjusted[i]/season[int(m[5:])-1])
    fallback = median(available_rates[-12:])
    result = []
    for i, m in enumerate(months):
        days = month_days(m)
        missing = min(days, max(0, number(item.get('stockout_days', {}).get(m))))
        corrected = adjusted[i]
        if compensate and missing > 0:
            if missing < days and corrected > 0:
                corrected *= min(3, days/(days-missing))
            else:
                corrected = max(corrected, fallback*season[int(m[5:])-1])
        result.append(dict(month=m, raw=max(0, number(item['sales'][m])),
                           regular=round(adjusted[i], 4), corrected=round(corrected, 4),
                           removed=round(excluded[i], 4), lost=round(corrected-adjusted[i], 4),
                           stockout_days=missing, candidate_regular=round(candidate[i], 4),
                           candidate_removed=round(detected_removed[i], 4), decision=decisions[i],
                           anomaly_reason='; '.join(reasons[i])))
    return result


def forecast_level(history, season, use_trend=True):
    """Monthly normalized demand before explicit future business assumptions."""
    norm = [h['corrected']/season[int(h['month'][5:])-1] for h in history]
    # Keep zero-demand months. Start at the first observed positive month.
    first = next((i for i, v in enumerate(norm) if v > 0), len(norm))
    active = norm[first:][-12:]
    weights = list(range(1, len(active)+1))
    base = sum(v*w for v, w in zip(active, weights))/sum(weights) if weights else 0
    growth = 0.0
    if use_trend and len(active) >= 6:
        before, recent = median(active[-6:-3]), median(active[-3:])
        # Two of the last three months must support a sustained change.
        if before > 0 and (sum(v > before*1.05 for v in active[-3:]) >= 2 or sum(v < before*.95 for v in active[-3:]) >= 2):
            growth = max(-.3, min(.3, (recent/before-1)*.5))
    return base, growth, active


def normalize_season(values):
    if len(values) != 12 or any(number(v) <= 0 for v in values):
        raise ValueError('Нужны 12 положительных коэффициентов сезонности')
    avg = sum(values)/12
    return [v/avg for v in values]


def calculate(item, season, settings):
    as_of = date.fromisoformat(settings.get('as_of', '2026-09-22'))
    season = normalize_season(season) if settings.get('seasonality', True) else [1.0]*12
    history = build_history(item, season, as_of, settings.get('remove_outliers', True), settings.get('compensate_stockouts', True))
    base, growth, active = forecast_level(history, season, settings.get('trend', True))
    # Explicit business forecast is a separate scenario driver, not a second copy of historical growth.
    business_growth = number(settings.get('growth_pct', 0))/100
    category_policy = settings.get('category_policies', {}).get(item.get('category', ''), {})
    supplier_policy = settings.get('supplier_policies', {}).get(item.get('supplier', ''), {})
    business_growth += number(category_policy.get('growth_pct', 0))/100
    lead = int(category_policy.get('lead_days', supplier_policy.get('lead_days', settings.get('lead_days', 30))))
    review = int(supplier_policy.get('review_days', settings.get('review_days', 14)))
    safety_days = int(category_policy.get('safety_days', supplier_policy.get('safety_days', settings.get('safety_days', 7))))
    horizon = lead+review
    if not (1 <= lead <= 180 and 1 <= review <= 90 and 0 <= safety_days <= 90 and -.9 <= business_growth <= 3):
        raise ValueError('Проверьте сроки поставки, период заказа, запас и прирост')
    monthly = max(0, base*(1+growth)*(1+business_growth))
    def demand_for(days):
        total = 0
        for d in range(days):
            day = as_of+timedelta(days=d)
            total += monthly*season[day.month-1]/calendar.monthrange(day.year, day.month)[1]
        return total
    demand = demand_for(horizon)
    safety = demand_for(horizon+safety_days)-demand
    total_transit = sum(number(t['qty']) for t in item.get('transit', []))
    eligible, overdue = 0.0, 0.0
    for t in item.get('transit', []):
        eta = date.fromisoformat(t['eta']) if t.get('eta') else None
        if eta and as_of <= eta < as_of+timedelta(days=horizon):
            eligible += number(t['qty'])
        elif eta and eta < as_of:
            overdue += number(t['qty'])
    stock = item.get('stock')
    stock_date = item.get('stock_date')
    stock_age = (as_of-date.fromisoformat(stock_date)).days if stock_date else None
    stale = stock is not None and (stock_age is None or stock_age > 3 or stock_age < 0)
    warnings = list(item.get('warnings', []))
    if stock is None:
        warnings.append('Нет числового остатка. Количество заказа не рассчитано')
    elif stock_age is None:
        warnings.append('Не указана дата остатка; подтвердите актуальный снимок')
    elif stock_age is not None and stock_age < 0:
        warnings.append(f'Остаток на {stock_date} позже даты расчёта; нужен снимок на выбранную дату')
    elif stale:
        warnings.append(f'Остаток на {stock_date}; расчёт предварительный')
    if not item.get('pack'):
        warnings.append('Нет кратности: для черновика принята 1')
    if overdue:
        warnings.append('Просроченный товар в пути исключён до сверки поступления')
    if any(not t.get('eta') for t in item.get('transit', [])):
        warnings.append('Поставка без даты не уменьшает потребность')
    if not active:
        warnings.append('Нет положительной истории спроса')
    pack = number(item.get('pack'), 1)
    pack = pack if pack > 0 else 1
    moq = max(0, number(item.get('moq')))
    net = max(0, demand+safety-max(0, number(stock))-eligible) if stock is not None else None
    qty = math.ceil(max(net, moq)/pack-1e-10)*pack if net is not None and net > 1e-8 else (0 if net is not None else None)
    # Project day-by-day balances. A later receipt cannot prevent an earlier stockout.
    shortage = None
    if stock is not None and monthly > 0:
        balance = max(0, number(stock))
        receipts = defaultdict(float)
        for t in item.get('transit', []):
            if t.get('eta') and t['eta'] >= as_of.isoformat():
                receipts[t['eta']] += number(t['qty'])
        for d in range(horizon):
            day = as_of+timedelta(days=d)
            balance += receipts[day.isoformat()]
            balance -= monthly*season[day.month-1]/calendar.monthrange(day.year, day.month)[1]
            if balance < -1e-8:
                shortage = d
                break
    urgency = 'Нет остатка' if stock is None else 'Срочно' if shortage is not None and shortage < lead else 'Планово' if qty else 'Достаточно'
    if shortage is not None and shortage < lead:
        warnings.append('Дефицит до новой поставки: требуется ускорение или перемещение')
    status = 'Нужны данные' if stock is None else 'Предварительно' if stale or not item.get('pack') else 'Черновик'
    reason = (f'Спрос на {horizon} дн. {demand:.1f} + запас {safety:.1f} − остаток '
              f'{number(stock):.1f} − поставки в горизонте {eligible:.1f}; кратность {pack:g}. '
              f'Тренд {growth:+.0%}, сценарий {business_growth:+.0%}.') if stock is not None else 'Нужен актуальный остаток для расчёта количества.'
    return dict(id=item['id'], supplier=item['supplier'], code=item['code'], article=item.get('article', ''),
                name=item.get('name', ''), unit=item.get('unit', ''), category=item.get('category', 'Без категории'),
                qty=qty, net=round(net, 3) if net is not None else None, stock=stock, stock_date=stock_date,
                demand=round(demand, 3), safety=round(safety, 3), monthly=round(monthly, 3),
                in_transit=round(eligible, 3), total_transit=total_transit, pack=pack, moq=moq,
                growth=round(growth, 4), business_growth=business_growth, urgency=urgency, status=status,
                shortage_days=shortage, lead_days=lead, review_days=review,
                safety_days=safety_days, horizon_days=horizon,
                removed=round(sum(h['removed'] for h in history[-12:]), 3),
                lost=round(sum(h['lost'] for h in history[-12:]), 3),
                reason=reason, warnings=list(dict.fromkeys(warnings)), history=history,
                transit=item.get('transit', []), sources=item.get('sources', []),
                source_growth=item.get('source_growth'), season=season,
                anomalies=item.get('anomalies', [])[:30])


def calculate_all(dataset, settings):
    rows = [calculate(item, dataset['seasonality'][item['supplier']], settings) for item in dataset['items']]
    rank = {'Срочно': 0, 'Нет остатка': 1, 'Планово': 2, 'Достаточно': 3}
    rows.sort(key=lambda r: (rank[r['urgency']], r['supplier'], -(r['qty'] or 0), r['code']))
    return rows
