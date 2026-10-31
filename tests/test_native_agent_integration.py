# tests/test_native_agent_integration.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Native account routes stay separate from APIs and unsupported wrappers."""
from __future__ import annotations

import hashlib
import json
import sys

import pytest

from core.app.providers import cli_discovery as llm_cli_discovery, discovery as llm_discovery
from core.app.settings.config import load_settings_inventory, selected_verify_environment


@pytest.mark.parametrize(('provider', 'backend', 'model_env'), [
    ('gemini_cli', 'gemini_cli', 'GEMINI_CLI_MODEL'),
    ('antigravity', 'antigravity_cli', 'ANTIGRAVITY_CLI_MODEL'),
    ('opencode_cli', 'opencode_cli', 'OPENCODE_CLI_MODEL'),
])
def test_catalog_produces_explicit_native_roles_without_api_credentials(
    monkeypatch, provider, backend, model_env,
):
    observed = []
    native_model = 'opencode-go/model' if backend == 'opencode_cli' else 'native-model'

    def catalog(name, environ):
        observed.append(name)
        return {'ok': True, 'installed': True, 'models': [native_model],
                'authenticated': None, 'message': 'Catalog only; response checked before Verify.'}

    monkeypatch.setattr(llm_cli_discovery, 'cli_catalog', catalog)
    original = {'GEMINI_API_KEY': 'unrelated-secret', 'GEMINI_MODEL': 'api-model'}
    result = llm_discovery.check_provider(provider, original)
    assert observed == [backend]
    assert result['ok'] is True and result['model_env'] == model_env
    assert 'authenticated' not in result
    assert result['options'][0]['selector'] == backend + ':' + native_model
    assert 'unrelated-secret' not in repr(result)
    selected = selected_verify_environment(
        {model_env: native_model}, {'jury1': [backend + ':' + native_model], 'jury2': []},
        jury2_level='off',
    )
    assert selected['available'] is True
    assert selected['overlay']['CITATION_VERIFIER_VERIFY_BACKENDS'] == backend
    assert original == {'GEMINI_API_KEY': 'unrelated-secret', 'GEMINI_MODEL': 'api-model'}


@pytest.mark.parametrize('provider', ['cursor_cli', 'copilot_cli'])
def test_unqualified_agents_never_become_api_or_other_cli_routes(monkeypatch, provider):
    monkeypatch.setattr(llm_discovery, '_installed', lambda _name, _environ=None: True)
    monkeypatch.setattr(llm_cli_discovery, 'cli_catalog',
                        lambda *_args: pytest.fail('Unqualified route must not launch an agent'))
    monkeypatch.setattr(llm_discovery, '_probe',
                        lambda *_args: pytest.fail('Must not substitute an API provider'))
    result = llm_discovery.check_provider(provider, {'OPENAI_API_KEY': 'private'})
    assert result['installed'] is True and result['ok'] is False
    assert result['models'] == [] and result['options'] == []
    assert 'private' not in repr(result)


def test_native_settings_have_separate_provider_groups_and_checks(tmp_path):
    template = tmp_path / '.env.example'
    template.write_text('GEMINI_MODEL=\nGEMINI_CLI_MODEL=\nANTIGRAVITY_CLI_MODEL=\nOPENCODE_MODEL=\nOPENCODE_CLI_MODEL=\n', encoding='utf-8')
    rows = {row['name']: row for row in load_settings_inventory(
        env_path=None, template_path=template, environ={},
    )}
    assert rows['GEMINI_CLI_MODEL']['model_provider'] == 'gemini_cli'
    assert rows['GEMINI_CLI_MODEL']['check_provider'] == 'gemini_cli'
    assert rows['GEMINI_CLI_MODEL']['provider'] != rows['GEMINI_MODEL']['provider']
    assert rows['ANTIGRAVITY_CLI_MODEL']['check_provider'] == 'antigravity'
    assert rows['OPENCODE_CLI_MODEL']['check_provider'] == 'opencode_cli'
    assert rows['OPENCODE_CLI_MODEL']['model_provider'] == 'opencode_cli'
    assert rows['OPENCODE_CLI_MODEL']['provider'] != rows['OPENCODE_MODEL']['provider']


def test_large_prompt_uses_bounded_stdin_and_preserves_its_bytes(tmp_path):
    prompt = 'Quoted manuscript text: "αβ" & $()\n' * 3000
    code = 'import sys,json,hashlib; b=sys.stdin.buffer.read(); print(json.dumps({"sha256":hashlib.sha256(b).hexdigest()}),flush=True)'
    with llm_cli_discovery._Session([sys.executable, '-u', '-c', code], {}, cwd=tmp_path) as session:
        session.send_text(prompt)
        session.process.stdin.close()
        result = session.document()
        assert session.process.wait(timeout=2) == 0
    assert result == {'sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest()}
    assert not session.reader.is_alive()
    assert not session.error_reader.is_alive()


def test_cli_that_never_reads_stdin_cannot_block_a_request_indefinitely(tmp_path):
    code = 'import time; time.sleep(20)'
    session = llm_cli_discovery._Session(
        [sys.executable, '-u', '-c', code], {}, cwd=tmp_path, timeout=0.2,
    )
    with pytest.raises(llm_cli_discovery._CatalogError, match='timed out'):
        with session:
            session.send_text('x' * (1024 * 1024))
    assert session.process.poll() is not None
    assert not session.reader.is_alive()
    assert not session.error_reader.is_alive()


_GEMINI_FIXTURE = r'''
import hashlib,json,os,pathlib,sys
mode,kind,transcript=sys.argv[1:]
settings=json.loads(pathlib.Path(os.environ['GEMINI_CLI_SYSTEM_SETTINGS_PATH']).read_text())
assert settings['tools']['core']==[]
assert settings['hooksConfig']['enabled'] is False
assert settings['admin']['mcp']['enabled'] is False
assert not os.environ.get('GEMINI_API_KEY') and not os.environ.get('GOOGLE_API_KEY')
if mode=='auth':
    print('Authentication required: SECRET-NATIVE-LOGIN',file=sys.stderr,flush=True)
    sys.exit(1)
if kind=='prompt':
    text=sys.stdin.read()
    pathlib.Path(transcript).write_text(json.dumps({'sha256':hashlib.sha256(text.encode()).hexdigest()}))
    print(json.dumps({'response':'{"answer":"fixture"}','stats':{'models':{'gemini-fixture-v1':{}},'tools':{'totalCalls':0}}}),flush=True)
else:
    for line in sys.stdin:
        request=json.loads(line)
        with open(transcript,'a',encoding='utf-8') as handle:handle.write(json.dumps(request)+'\n')
        assert request['jsonrpc']=='2.0'
        if request['method']=='initialize':
            result={'protocolVersion':1,'authMethods':[{'id':'oauth-personal'}]}
        elif request['method']=='session/new':
            assert request['params']['mcpServers']==[]
            result={'sessionId':'fixture','models':{'availableModels':[{'modelId':'gemini-fixture-v1'}]}}
        else:raise RuntimeError('Unexpected agent operation')
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
'''


@pytest.mark.parametrize('mode', ['prompt', 'catalog', 'auth'])
def test_gemini_native_protocol_uses_real_bounded_pipes_without_provider_calls(tmp_path, monkeypatch, mode):
    from core.verify.backends import _gemini_cli_runtime as runtime
    from core.verify.backends.errors import AuthError

    fixture = tmp_path / 'native_gemini.py'
    fixture.write_text(_GEMINI_FIXTURE, encoding='utf-8')
    transcript = tmp_path / 'transcript.jsonl'
    sessions = []

    def session(argv, environ, **kwargs):
        kind = 'catalog' if '--acp' in argv else 'prompt'
        instance = llm_cli_discovery._Session(
            [sys.executable, '-u', str(fixture), mode, kind, str(transcript)], environ, **kwargs,
        )
        sessions.append(instance)
        return instance

    monkeypatch.setattr(runtime, '_Session', session)
    env = {'GEMINI_API_KEY': 'api-secret', 'GOOGLE_API_KEY': 'other-api-secret'}
    if mode == 'catalog':
        result = runtime.check_catalog('gemini', env, temp_root=tmp_path)
        assert result['ok'] is True and result['authenticated'] is True
        assert result['models'] == ['gemini-fixture-v1']
        requests = [json.loads(line) for line in transcript.read_text().splitlines()]
        assert [request['method'] for request in requests] == ['initialize', 'session/new']
    elif mode == 'auth':
        with pytest.raises(AuthError) as caught:
            runtime.run_gemini_prompt('gemini', 'system', 'source', 'gemini-fixture-v1',
                                      timeout=5, environ=env, temp_root=tmp_path)
        assert 'SECRET-NATIVE-LOGIN' not in str(caught.value)
    else:
        source = 'Quoted text αβ " & $()\n' * 5000
        answer = runtime.run_gemini_prompt('gemini', 'system', source, 'gemini-fixture-v1',
                                          timeout=5, environ=env, temp_root=tmp_path)
        assert answer == '{"answer":"fixture"}'
        assert json.loads(transcript.read_text())['sha256'] == hashlib.sha256(
            ('system\n\n' + source).encode('utf-8'),
        ).hexdigest()
    assert sessions and all(item.process.poll() is not None for item in sessions)
    assert all(not item.reader.is_alive() and not item.error_reader.is_alive() for item in sessions)
    assert not list(tmp_path.glob('callimachus-gemini-*'))
    assert env == {'GEMINI_API_KEY': 'api-secret', 'GOOGLE_API_KEY': 'other-api-secret'}


def test_native_discovery_attempts_are_listed_without_submitting_user_turns(monkeypatch):
    checked = []

    def check(provider, _environ):
        checked.append(provider)
        return {'provider': provider, 'ok': False, 'models': [], 'options': []}

    monkeypatch.setattr(llm_discovery, 'check_provider', check)
    result = llm_discovery.discover_local_providers({})
    assert checked == ['claude_cli', 'codex_cli', 'gemini_cli', 'antigravity',
                       'opencode_cli', 'cursor_cli', 'copilot_cli', 'ollama', 'freetoken']
    assert all(not provider['options'] for provider in result['providers'])
