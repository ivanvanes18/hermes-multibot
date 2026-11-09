"""Configuration rejections through the real loader, not a fabricated context."""
import copy
import pytest
from conftest import HOME, CONFIG, yaml_dump

@pytest.mark.parametrize('bots', [
    [{'slot':1,'alias':'alpha','allowed_users':['42']}, {'slot':1,'alias':'alpha','allowed_users':['43']}],
    [{'slot':1,'alias':'INVALID','allowed_users':['42']}],
    [{'slot':1,'alias':'alpha','allowed_users':[]}],
    [{'slot':1,'alias':'alpha','allowed_users':['*']}],
    [{'slot':1,'alias':'alpha','allowed_users':[42]}],
    [{'slot':1,'alias':'alpha','allowed_users':['@someone']}],
    [{'slot':0,'alias':'work','allowed_users':['42']}],
    [{'slot':9,'alias':'work','allowed_users':['42']}],
    [{'slot':True,'alias':'work','allowed_users':['42']}],
    [{'slot':1,'alias':'work','allowed_users':['42'],'allow_all':True}],
    [{'slot':1,'alias':'work','allowed_users':['42'],'token':'forbidden'}],
    [{'slot':1,'alias':'UPPER','allowed_users':['42']}],
    [{'slot':1,'alias':'a'*25,'allowed_users':['42']}],
    [{'slot':1,'alias':'work','allowed_users':['42']}, {'slot':1,'alias':'other','allowed_users':['43']}],
    [{'slot':1,'alias':'work','allowed_users':['42']}, {'slot':2,'alias':'work','allowed_users':['43']}],
])
def test_bad_policy_fails_closed(bots):
    from hermes_cli.plugins import get_plugin_manager
    from gateway.platform_registry import platform_registry
    data = copy.deepcopy(CONFIG)
    data['plugins']['entries']['hermes-multibot']['settings']['bots'] = bots
    (HOME/'config.yaml').write_text(yaml_dump(data))
    manager = get_plugin_manager()
    try:
        manager.discover_and_load(force=True)
        entry = manager._plugins['hermes-multibot']
        assert entry.error and not entry.enabled
        assert all(platform_registry.get(f'telegram_{a}') is None for a in ['alpha','beta','gamma'])
    finally:
        (HOME/'config.yaml').write_text(yaml_dump(CONFIG))
        manager.discover_and_load(force=True)


def test_all_eight_slots_with_arbitrary_valid_aliases():
    from hermes_cli.plugins import get_plugin_manager
    from gateway.platform_registry import platform_registry
    data = copy.deepcopy(CONFIG)
    data['plugins']['entries']['hermes-multibot']['settings']['bots'] = [
        {'slot': i, 'alias': f'work_{i}', 'allowed_users': ['123456789']} for i in range(1, 9)
    ]
    (HOME/'config.yaml').write_text(yaml_dump(data))
    manager = get_plugin_manager()
    try:
        manager.discover_and_load(force=True)
        entry = manager._plugins['hermes-multibot']
        assert entry.enabled and not entry.error
        assert all(platform_registry.get(f'telegram_work_{i}') is not None for i in range(1, 9))
    finally:
        (HOME/'config.yaml').write_text(yaml_dump(CONFIG))
        manager.discover_and_load(force=True)
