def to_ordinal(year, month, day):
    month = min(max(month, 1), 12)
    day = min(max(day, 1), 31)
    return year * 372 + (month - 1) * 31 + (day - 1)


def from_ordinal(ordinal):
    year = ordinal // 372
    rem = ordinal % 372
    month = rem // 31 + 1
    day = rem % 31 + 1
    return year, month, day


def merge_and_split_periods(periods, field_key):
    """Мерджит пересекающиеся периоды с одинаковым field_key,
    а пересекающиеся периоды с разными field_key обрезает по середине пересечения
    (при полном вложении одного периода в другой - вырезает в объемлющем периоде дыру)."""
    if not periods:
        return periods

    segments = []
    for p in periods:
        segments.append({
            "start": to_ordinal(p["start_year"], p.get("start_month", 1), p.get("start_day", 1)),
            "end": to_ordinal(p["end_year"], p.get("end_month", 12), p.get("end_day", 31)),
            "field": p[field_key],
            "data": dict(p),
        })

    # Мердж пересекающихся/соприкасающихся периодов с одинаковым field_key
    segments.sort(key=lambda x: (x["field"], x["start"]))
    merged = []
    for seg in segments:
        if merged and merged[-1]["field"] == seg["field"] and seg["start"] <= merged[-1]["end"] + 1:
            merged[-1]["end"] = max(merged[-1]["end"], seg["end"])
            if seg["end"] >= merged[-1]["end"]:
                merged[-1]["data"] = seg["data"]
        else:
            merged.append(seg)

    # Разделение пересекающихся периодов с разными field_key
    changed = True
    while changed:
        changed = False
        merged.sort(key=lambda x: x["start"])
        for i in range(len(merged) - 1):
            cur, nxt = merged[i], merged[i + 1]
            if cur["field"] == nxt["field"] or cur["end"] < nxt["start"]:
                continue
            if cur["end"] <= nxt["end"]:
                # обычное пересечение краями: обрезаем обе записи по середине пересечения
                mid = (nxt["start"] + cur["end"]) // 2
                cur["end"] = mid
                nxt["start"] = mid + 1
            else:
                # nxt целиком внутри cur: вырезаем в cur дыру под nxt
                before = dict(cur)
                before["end"] = nxt["start"] - 1
                after = dict(cur)
                after["start"] = nxt["end"] + 1
                merged[i] = before
                merged.insert(i + 2, after)
            changed = True
            break
        merged = [seg for seg in merged if seg["start"] <= seg["end"]]

    result = []
    for seg in merged:
        start_year, start_month, start_day = from_ordinal(seg["start"])
        end_year, end_month, end_day = from_ordinal(seg["end"])
        record = dict(seg["data"])
        record["start_year"], record["start_month"], record["start_day"] = start_year, start_month, start_day
        record["end_year"], record["end_month"], record["end_day"] = end_year, end_month, end_day
        result.append(record)

    return sorted(result, key=lambda x: (x["start_year"], x["start_month"], x["start_day"]))
