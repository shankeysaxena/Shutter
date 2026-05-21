"""Tests for Zerodha auth helpers (all mocked — no real API calls)."""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("kiteconnect")

from src.integrations.zerodha.config import KiteConfig


@pytest.fixture
def cfg():
    return KiteConfig(api_key='test_key', api_secret='test_secret')


class TestGetLoginUrl:
    def test_returns_kite_login_url(self, cfg):
        from src.integrations.zerodha.auth import get_login_url
        # Patch at the kiteconnect package level — lazy import resolves there
        with patch('kiteconnect.KiteConnect') as MockKite:
            MockKite.return_value.login_url.return_value = 'https://kite.zerodha.com/connect/login?api_key=test_key&v=3'
            url = get_login_url(cfg)
        assert 'kite.zerodha.com' in url
        assert 'test_key' in url


class TestExchangeToken:
    def test_exchanges_and_saves_token(self, cfg, tmp_path, monkeypatch):
        from src.integrations.zerodha import auth as auth_mod
        monkeypatch.setattr(auth_mod, 'TOKEN_FILE', tmp_path / '.kite_token.json')

        with patch('kiteconnect.KiteConnect') as MockKite:
            MockKite.return_value.generate_session.return_value = {'access_token': 'abc123'}
            token = auth_mod.exchange_token(cfg, 'request_tok')

        assert token == 'abc123'
        saved = json.loads((tmp_path / '.kite_token.json').read_text())
        assert saved['access_token'] == 'abc123'
        assert saved['api_key'] == 'test_key'

    def test_saved_token_roundtrip(self, cfg, tmp_path, monkeypatch):
        from src.integrations.zerodha import auth as auth_mod
        monkeypatch.setattr(auth_mod, 'TOKEN_FILE', tmp_path / '.kite_token.json')

        with patch('kiteconnect.KiteConnect') as MockKite:
            MockKite.return_value.generate_session.return_value = {'access_token': 'tok999'}
            auth_mod.exchange_token(cfg, 'req')

        loaded = auth_mod.load_saved_token(cfg)
        assert loaded == 'tok999'

    def test_load_returns_none_when_file_missing(self, cfg, tmp_path, monkeypatch):
        from src.integrations.zerodha import auth as auth_mod
        monkeypatch.setattr(auth_mod, 'TOKEN_FILE', tmp_path / 'nonexistent.json')
        assert auth_mod.load_saved_token(cfg) is None

    def test_load_returns_none_when_api_key_mismatch(self, cfg, tmp_path, monkeypatch):
        from src.integrations.zerodha import auth as auth_mod
        p = tmp_path / '.kite_token.json'
        p.write_text(json.dumps({'api_key': 'other_key', 'access_token': 'x'}))
        monkeypatch.setattr(auth_mod, 'TOKEN_FILE', p)
        assert auth_mod.load_saved_token(cfg) is None

    def test_load_tolerates_corrupt_file(self, cfg, tmp_path, monkeypatch):
        from src.integrations.zerodha import auth as auth_mod
        p = tmp_path / '.kite_token.json'
        p.write_text('not json {{{')
        monkeypatch.setattr(auth_mod, 'TOKEN_FILE', p)
        assert auth_mod.load_saved_token(cfg) is None
