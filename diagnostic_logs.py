"""固定服务日志的限量投影与保守过滤；不读取任意文件或执行日志内容。"""
import json
import re
import shlex
from device_registry import PROFILES

LOG_LIMIT = 40
RAW_LIMIT = 262144
SAFE_LIMIT = 12000
UNITS = {
    'local': PROFILES['local_ha']['units'],
    'remote': PROFILES['remote_ha']['units'],
    'router': PROFILES['router_ha']['units'],
}
SENSITIVE = re.compile(r'password|passwd|\bpwd\b|secret|token|api[_ -]?key|authorization|credential|'
                       r'密码|口令|密钥|令牌|\bBearer\s+|\bsk-[A-Za-z0-9_-]+|'
                       r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|://[^/\s]+@|'
                       r'(?:^|\s)[A-Za-z0-9+/]{48,}={0,2}(?:\s|$)', re.I)
PRIVATE_KEY_BEGIN = re.compile(r'-----BEGIN[^\n]*PRIVATE KEY-----')
PRIVATE_KEY_END = re.compile(r'-----END[^\n]*PRIVATE KEY-----')
CREDENTIAL_VALUE = re.compile(
    r'(?i)(?:\b(?:password|passwd|pwd|secret|token|api[_ -]?key|credential)\b|密码|口令|密钥|令牌)'
    r'\s*["\x27]?\s*[:=]\s*(?:"([^"\r\n]+)"|\x27([^\x27\r\n]+)\x27|([^\s"\x27,;，；]+))'
    r'|(?:\bBearer\s+|\bauthorization\b["\x27]?\s*[:=]\s*["\x27]?(?:Basic|Bearer)\s+)([^\s"\x27,;，；]+)'
)


def credential_values(output):
    values = {next(value for value in match.groups() if value)
              for match in CREDENTIAL_VALUE.finditer(output)}
    values |= {value.rstrip('.。') for value in values if value.endswith(('.', '。'))}
    values.discard('')
    return values


def filter_log_output(output):
    """Filter already projected worker logs at the host boundary as well."""
    values = credential_values(output)
    lines = []
    in_key = False
    for line in output.splitlines(keepends=True):
        begin = bool(PRIVATE_KEY_BEGIN.search(line))
        end = bool(PRIVATE_KEY_END.search(line))
        if in_key or begin or SENSITIVE.search(line):
            lines.append('[含敏感字段或密钥块的日志消息已隐藏]\n')
        else:
            lines.append(line)
        in_key = (in_key or begin) and not end
    filtered = ''.join(lines)
    for value in sorted(values, key=len, reverse=True):
        if len(value) >= 4:
            filtered = filtered.replace(value, '[敏感值已隐藏]')
        else:
            filtered = re.sub(short_credential_pattern(value), '[敏感值已隐藏]', filtered)
    return filtered, values


def short_credential_pattern(value):
    # Chinese prose can surround the credential with no whitespace.
    return r'(?<![A-Za-z0-9_])' + re.escape(value) + r'(?![A-Za-z0-9_])'


def echoes_log_credential(answer, values):
    return any(value in answer if len(value) >= 4 else re.search(short_credential_pattern(value), answer)
               for value in values)


def selected_units(remote,group):
    group=group if group is not None else 'remote' if remote else 'local'
    if not isinstance(group,str) or group not in UNITS:raise ValueError('Unknown log group')
    return UNITS[group]


def log_command(remote=False,group=None):
    units = selected_units(remote,group)
    parts = ['journalctl', '-b', '-n', str(LOG_LIMIT), '--no-pager', '--quiet', '-o', 'json']
    for unit in units:
        parts.extend(['-u', unit])
    return ' '.join(shlex.quote(part) for part in parts)


def project_logs(raw, remote=False,group=None):
    """仅保留指定服务、设备日志时间和消息；其他journal字段不返回宿主。"""
    if len(raw) > RAW_LIMIT:
        raise ValueError('Log output too large')
    units = selected_units(remote,group)
    lines = raw.decode('utf-8').splitlines()
    if len(lines) > LOG_LIMIT:
        raise ValueError('Too many log entries')
    projected = []
    in_key = False
    redacted = 0
    records = [json.loads(line) for line in lines]
    if any(not isinstance(row, dict) for row in records):
        raise ValueError('Invalid log entry')
    values = {value for row in records if isinstance(row.get('MESSAGE'), str)
              for value in credential_values(row['MESSAGE'])}
    for row in records:
        unit = next((row.get(key) for key in ('UNIT', '_SYSTEMD_UNIT', 'OBJECT_SYSTEMD_UNIT')
                     if isinstance(row.get(key), str) and row[key] in units), 'unconfirmed')
        stamp = row.get('__REALTIME_TIMESTAMP', '')
        stamp = stamp if isinstance(stamp, str) and re.fullmatch(r'[0-9]{1,20}', stamp) else 'unknown'
        message = row.get('MESSAGE')
        if not isinstance(message, str):
            message = '[非文本或过长日志消息，已省略]'
        begin = bool(PRIVATE_KEY_BEGIN.search(message))
        end = bool(PRIVATE_KEY_END.search(message))
        if in_key or begin or SENSITIVE.search(message) or echoes_log_credential(message, values):
            redacted += 1
            safe = '[含敏感字段或密钥块的日志消息已隐藏]'
        else:
            safe = ' '.join(message.split())
            if len(safe) > 240:
                safe = safe[:240] + ' [消息已截短]'
        in_key = (in_key or begin) and not end
        projected.append('[unit=' + unit + ' device_time_us=' + stamp + '] ' + safe)
    # 优先保留最新事件，投影结果和worker原始输出分别限制大小。
    selected = []
    size = 0
    for line in reversed(projected):
        cost = len((line+'\n').encode('utf-8'))
        if size + cost > SAFE_LIMIT - 1000:
            break
        selected.append(line)
        size += cost
    header = ('LOG_VIEW=current_boot_tail\nLOG_UNITS=' + ','.join(units) +
              '\nLOG_RECORDS_RETURNED=' + str(len(selected)) +
              '\nLOG_RECORDS_OMITTED=' + str(len(projected)-len(selected)) +
              '\nLOG_MESSAGES_REDACTED=' + str(redacted) +
              '\n日志是本次采集看到的当前启动期间历史事件；设备日志时钟未校准，不代表当前服务状态。\n')
    return header + '\n'.join(reversed(selected))
