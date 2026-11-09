import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(tempfile.mkdtemp(prefix='multibot-'))
os.environ['HERMES_HOME'] = str(HOME)
os.environ['HOME'] = str(HOME)
os.environ['MULTIBOT_SLOT_1_TOKEN'] = '10001:FAKE_ALPHA_TOKEN_OFFLINE_ONLY'
os.environ['MULTIBOT_SLOT_2_TOKEN'] = '10002:FAKE_BETA_TOKEN_OFFLINE_ONLY'
os.environ['MULTIBOT_SLOT_3_TOKEN'] = '10003:FAKE_GAMMA_TOKEN_OFFLINE_ONLY'
os.environ['OPENAI_API_KEY'] = 'fake-offline-test-key'
os.environ['HERMES_DISABLE_TELEMETRY'] = '1'
from ruamel.yaml import YAML
from io import StringIO

def yaml_dump(value):
    stream = StringIO()
    YAML().dump(value, stream)
    return stream.getvalue()
CONFIG = {
    'model': {'default': 'gpt-4.1-mini', 'provider': 'openai', 'api_mode': 'chat_completions'},
    'toolsets': [], 'platform_toolsets': {},
    'display': {'tool_progress': 'off', 'streaming': False},
    'plugins': {'enabled': ['hermes-multibot'], 'entries': {'hermes-multibot': {'enabled': True, 'settings': {'bots': [
        {'slot': 1, 'alias': 'alpha', 'allowed_users': ['42', '43']},
        {'slot': 2, 'alias': 'beta', 'allowed_users': ['42', '44']},
        {'slot': 3, 'alias': 'gamma', 'allowed_users': ['45']},
    ]}}}},
    'gateway': {'platforms': {f'telegram_{a}': {'enabled': True} for a in ['alpha','beta','gamma']}},
}
(HOME / 'config.yaml').write_text(yaml_dump(CONFIG))
(HOME / 'memories').mkdir()
(HOME / 'memories' / 'MEMORY.md').write_text('OFFLINE_SHARED_MEMORY_SENTINEL is a test fixture fact.')
skill_dir = HOME / 'skills' / 'offline-shared-skill'
skill_dir.mkdir(parents=True)
(skill_dir / 'SKILL.md').write_text(
    '---\nname: offline-shared-skill\ndescription: OFFLINE_SHARED_SKILL_SENTINEL test fixture.\n---\n'
    '# Offline fixture\nNo operations are performed by this fixture.\n'
)
(HOME / 'plugins').mkdir()
if not os.environ.get('MULTIBOT_BASELINE'):
    (HOME / 'plugins' / 'hermes-multibot').symlink_to(ROOT / 'plugin', target_is_directory=True)

import pytest
import socket

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('network prohibited in offline tests')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)
