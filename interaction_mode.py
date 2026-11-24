"""用户入口和当前可执行场景；选择入口不是设备批准。"""
MODES = ('qa', 'diagnostic', 'assistant')
from device_registry import scenes
SCENES = scenes()


def validate_selection(mode, scene):
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('Invalid interaction mode')
    if mode in ('qa','assistant'):
        if scene is not None: raise ValueError('QA has no diagnostic scene')
    elif not isinstance(scene, str) or scene not in SCENES:
        raise ValueError('Invalid diagnostic scene')


def allows_scope(scene, scope):
    allowed = SCENES.get(scene, {}).get('devices', ())
    return bool(scope) and all(device in allowed for device in scope)


def scene_catalog():
    return [{'id': key, 'title': value['title'], 'available': bool(value['devices']),
             'note': value['note']} for key, value in SCENES.items()]
