from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _parse_simple_yaml(text: str) -> dict[str, object]:
    result: dict[str, object] = {}
    current_list: list[str] | None = None
    current_key: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - "):
            if current_list is None or current_key is None:
                raise AssertionError(f"orphan list item: {line}")
            current_list.append(line[4:].strip())
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"')
        if value:
            result[key] = value
            current_list = None
            current_key = None
        else:
            current_list = []
            current_key = key
            result[key] = current_list
    return result


def test_plugin_metadata_declares_market_and_compatibility_fields() -> None:
    metadata = _parse_simple_yaml((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == "astrbot_plugin_pi_agent"
    assert metadata["version"] == "v0.3.7"
    assert metadata["author"] == "Yezi and Cz"
    assert metadata["astrbot_version"] == ">=4.27.1,<5"
    assert metadata["support_platforms"] == ["aiocqhttp"]
    assert metadata["tags"] == ["工具", "外部集成"]
    assert metadata["category"] == "integrations"
    assert "内置 Agent" in str(metadata["desc"])
