"""只解码完整JSON动作；不猜补截断正文、不从混杂文本中提取命令。"""
import json
import re


class ActionJSONError(ValueError):
    def __init__(self, kind, line=None, column=None, position=None):
        super().__init__(kind)
        self.kind = kind
        self.line, self.column, self.position = line, column, position


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ActionJSONError('duplicate_key')
        value[key] = item
    return value


def reject_constant(value):
    raise ActionJSONError('nonfinite_number')


def decode_action(content):
    if not isinstance(content, str):
        raise ActionJSONError('content_not_text')
    text = content.strip()
    fence = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', text, re.S | re.I)
    if fence:
        text = fence.group(1)
    try:
        value = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except json.JSONDecodeError as error:
        # 仅保留位置；错误消息/doc可能包含问题、设备输出或模型正文，不进入审计。
        raise ActionJSONError('action_syntax', error.lineno, error.colno, error.pos) from None
    except RecursionError:
        raise ActionJSONError('excessive_nesting') from None
    if not isinstance(value, dict):
        raise ActionJSONError('action_not_object')
    return value
