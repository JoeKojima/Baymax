"""Conservative local note formatting. Full source text/evidence stays unchanged."""
from datetime import datetime
import re

MONTHS = 'January February March April May June July August September October November December'.split()
_MONTH = '|'.join(MONTHS)
_DAY = r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'


def compact_note(text, people=(), kind='fact'):
    text = re.sub(r'\s+', ' ', str(text)).strip()
    # Only remove a subject when the single owner is separately represented.
    if len(people) == 1 and people[0]:
        name = re.escape(people[0])
        if kind == 'conversation':
            text = re.sub(r'^On [^,]+(?:, [^,]+)?,\s*'+name+
                          r' talked with Ember about\s+', 'Topics: ', text, flags=re.I)
        text = re.sub(r'^'+name+r'\s+(?=(?:is|was|has|had|uses|plays|enjoys|likes|loves|prefers|works|requested|clarified|studies|researches|wants|plans|learns)\b)', '', text, flags=re.I)

    def date(match):
        month, day, year = match.group(1, 2, 3)
        month_num = next(i+1 for i, m in enumerate(MONTHS) if m.lower() == month.lower())
        try:
            datetime(int(year or 2000), month_num, int(day))
        except ValueError:
            return match.group(0)
        # Do not guess the year of an event from its observation timestamp.
        return f'{year}-{month_num:02d}-{int(day):02d}' if year else f'{month[:3]} {int(day)}'
    text = re.sub(_DAY+r'('+_MONTH+r')\s+(\d{1,2})(?:,?\s+(\d{4}))?\b', date, text, flags=re.I)
    return text


def observed_date(timestamp):
    try:
        timestamp=float(timestamp)
        return datetime.fromtimestamp(timestamp).date().isoformat() if timestamp>0 else None
    except (TypeError, ValueError, OSError, OverflowError):
        return None
