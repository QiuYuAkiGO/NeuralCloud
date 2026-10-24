"""Read-only wall clock, independent of chat history and phone UI."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def current_time(now=None):
    instant = datetime.now(timezone.utc) if now is None else now
    if instant.tzinfo is None:
        raise ValueError('timezone_aware_datetime_required')
    utc = instant.astimezone(timezone.utc)
    local = utc.astimezone(ZoneInfo('Asia/Shanghai'))
    return {
        'timezone': 'Asia/Shanghai',
        'local_time': local.isoformat(timespec='seconds'),
        'date': local.date().isoformat(),
        'time': local.strftime('%H:%M:%S'),
        'weekday': ['星期一','星期二','星期三','星期四','星期五','星期六','星期日'][local.weekday()],
        'utc_time': utc.isoformat(timespec='seconds'),
        'unix_seconds': int(utc.timestamp()),
        'source': 'server_system_clock',
        'meaning': 'Time at this tool call, not a historical memory. Call again when a fresh current time is needed.'
    }
