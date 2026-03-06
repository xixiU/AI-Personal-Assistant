import os
import tempfile
import yaml
from ai_assistant.core.config import Config


def test_load_config_from_file():
    # 创建临时配置文件
    config_data = {
        "trigger": {"keyword": "【test】"},
        "context": {"max_messages": 5},
        "ai": {
            "primary": {
                "provider": "cherrystudio",
                "base_url": "http://localhost:8000"
            }
        }
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(config_data, f)
        temp_path = f.name

    try:
        config = Config.load(temp_path)
        assert config.trigger_keyword == "【test】"
        assert config.context_max_messages == 5
        assert config.ai_primary_provider == "cherrystudio"
    finally:
        os.unlink(temp_path)


def test_config_default_values():
    config = Config()
    assert config.trigger_keyword == "【ai】"
    assert config.context_max_messages == 10
    assert config.reply_mode == "clipboard"
