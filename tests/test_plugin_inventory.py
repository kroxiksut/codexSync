from dataclasses import asdict
import json
from pathlib import Path

from codexsync.plugin_inventory import read_plugin_inventory
from codexsync.preflight import _check_plugin_inventory


def _marker(root, relative):
    marker = root / relative
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"manifest-body-secret")
    return marker


def test_inventory_reports_strict_declarations_and_cache_provenance(tmp_path, monkeypatch, caplog):
    config = tmp_path / "config.toml"
    config.write_text(
        'secret = "root-config-secret"\n'
        '[plugins]\n'
        '"browser@openai-bundled" = { enabled = true }\n'
        '"missing@openai-bundled" = { enabled = false }\n'
        '"custom@private-market" = { enabled = true }\n'
        '"malformed-key-secret" = { enabled = true }\n'
        '"invalid@openai-bundled" = { enabled = "wrong-type-secret" }\n'
        '"extra@openai-bundled" = { enabled = true, source = "source-url-secret" }\n'
        '"configured@external-market" = { enabled = true }\n'
        '[marketplaces.external-market]\n'
        'source = "marketplace-path-secret"\n'
        'source_type = "marketplace-type-secret"\n',
        encoding="utf-8",
    )
    _marker(tmp_path, "plugins/cache/openai-bundled/browser/1/.codex-plugin/plugin.json")
    _marker(tmp_path, "plugins/cache/openai-bundled/browser/2/.codex-plugin/plugin.json")
    _marker(tmp_path, "plugins/cache/openai-bundled/cache-name-secret/1/.codex-plugin/plugin.json")
    _marker(tmp_path, "plugins/.remote-plugin-install-staging/a/1/.codex-plugin/plugin.json")
    _marker(tmp_path, "plugins/.plugin-appserver/a/1/.codex-plugin/plugin.json")
    _marker(tmp_path, "plugins/cache/openai-bundled/.staging/1/.codex-plugin/plugin.json")
    _marker(tmp_path, "plugins/cache/openai-bundled/unmarked/1/native.exe")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    original_open = Path.open

    def only_config_can_be_opened(path, mode="r", *args, **kwargs):
        assert path == config
        assert mode == "rb"
        return original_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", only_config_can_be_opened)
        inventory = read_plugin_inventory(tmp_path)
        check = _check_plugin_inventory(tmp_path)

    assert [asdict(item) for item in inventory.declarations] == [
        {"plugin_id": "browser", "marketplace_id": "openai-bundled", "enabled": True},
        {"plugin_id": "configured", "marketplace_id": "external-market", "enabled": True},
        {"plugin_id": "custom", "marketplace_id": "private-market", "enabled": True},
        {"plugin_id": "missing", "marketplace_id": "openai-bundled", "enabled": False},
    ]
    assert inventory.cached_declarations == 1
    assert inventory.configured_marketplace_declarations == 1
    assert inventory.findings == {
        "MALFORMED_PLUGIN_DECLARATION": 3,
        "DECLARED_PLUGIN_PAYLOAD_MISSING": 2,
        "CACHE_ONLY_PLUGIN_UNKNOWN_PROVENANCE": 1,
        "CUSTOM_PLUGIN_SOURCE_NOT_CONFIGURED": 1,
    }
    assert check.status == "WARN"
    for code in inventory.findings:
        assert code in check.details
    exposed = repr(inventory) + json.dumps(asdict(inventory)) + repr(check) + caplog.text
    assert "secret" not in exposed
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_malformed_toml_has_a_fixed_diagnostic(tmp_path):
    (tmp_path / "config.toml").write_text('plugins = { "syntax-secret" = ', encoding="utf-8")
    inventory = read_plugin_inventory(tmp_path)
    assert not inventory.declarations
    assert inventory.findings == {"PLUGIN_CONFIG_UNREADABLE": 1}
    assert "syntax-secret" not in repr(inventory)


def test_cache_accepts_hash_and_semver_hash_version_folders(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[plugins]\n'
        '"github@openai-curated" = { enabled = true }\n'
        '"github@openai-curated-remote" = { enabled = true }\n',
        encoding="utf-8",
    )
    _marker(tmp_path, "plugins/cache/openai-curated/github/bd2122cb/.codex-plugin/plugin.json")
    _marker(
        tmp_path,
        "plugins/cache/openai-curated-remote/github/0.1.13-77cafe/.codex-plugin/plugin.json",
    )

    inventory = read_plugin_inventory(tmp_path)

    assert inventory.cached_declarations == 2
    assert "DECLARED_PLUGIN_PAYLOAD_MISSING" not in inventory.findings


def test_cache_with_unusable_layout_does_not_claim_payload_is_missing(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[plugins]\n"browser@openai-bundled" = { enabled = true }\n', encoding="utf-8",
    )
    _marker(tmp_path, "plugins/cache")
    inventory = read_plugin_inventory(tmp_path)
    assert inventory.findings == {"PLUGIN_CACHE_UNREADABLE": 1}
