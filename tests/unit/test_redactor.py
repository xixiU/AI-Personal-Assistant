"""脱敏工具单元测试"""
from ai_assistant.utils.redactor import Redactor


def _make():
    return Redactor()


def test_known_secret_exact_mask():
    r = _make()
    r.register_secret("gho_realGitToken1234567890abcdef")
    out = r.redact("拉取失败，token 是 gho_realGitToken1234567890abcdef 请检查")
    assert "gho_realGitToken1234567890abcdef" not in out
    assert "【已隐藏】" in out


def test_short_secret_not_registered():
    r = _make()
    r.register_secret("abc")  # 长度 < 5，不登记
    assert r.secret_count == 0
    out = r.redact("abc def")
    assert out == "abc def"


def test_register_from_config_like_obj():
    r = _make()
    cfg = {
        "ai": {"primary": {"api_key": "sk-verysecretapikey12345", "model": "gpt-4"}},
        "repositories": [
            {"name": "backend", "auth_password": "P@ssw0rd-secret-value",
             "auth_username": "deploy_bot_user"}
        ],
    }
    r.register_from_obj(cfg)
    out = r.redact(
        "key=sk-verysecretapikey12345 pwd=P@ssw0rd-secret-value "
        "user=deploy_bot_user model=gpt-4"
    )
    assert "sk-verysecretapikey12345" not in out
    assert "P@ssw0rd-secret-value" not in out
    assert "deploy_bot_user" not in out
    assert "gpt-4" in out  # model 不是敏感键，保留


def test_ipv4_masked_with_whitelist():
    r = _make()
    out = r.redact("服务器 192.168.31.100 端口 8080，本地 127.0.0.1")
    assert "192.168.31.100" not in out
    assert "【已隐藏:IP】" in out
    assert "127.0.0.1" in out  # 白名单保留


def test_version_number_not_ip():
    r = _make()
    out = r.redact("智慧法庭 V4.0 组件版本 4.3.6")
    assert "4.3.6" in out  # 三段版本号不是 IPv4，不应误伤


def test_aws_ak_masked():
    r = _make()
    out = r.redact("AK 是 AKIAIOSFODNN7EXAMPLE 请配置")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "【已隐藏:AK】" in out


def test_jwt_masked():
    r = _make()
    jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
           "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
    out = r.redact(f"token: {jwt}")
    assert jwt not in out
    assert "【已隐藏:Token】" in out


def test_bearer_masked():
    r = _make()
    out = r.redact("Authorization: Bearer abcdef1234567890TOKEN")
    assert "abcdef1234567890TOKEN" not in out
    assert "Bearer 【已隐藏:Token】" in out


def test_url_embedded_credential_masked():
    r = _make()
    out = r.redact("git clone https://user:secretpass@git.internal.com/repo.git")
    assert "user:secretpass@" not in out
    assert "【已隐藏:凭据】@" in out


def test_password_assignment_masked():
    r = _make()
    out = r.redact('password: "MyRealPass123"\napi_key=liveKey987654')
    assert "MyRealPass123" not in out
    assert "liveKey987654" not in out


def test_empty_and_non_string():
    r = _make()
    assert r.redact("") == ""
    assert r.redact(None) is None


def test_idempotent():
    r = _make()
    r.register_secret("gho_realGitToken1234567890abcdef")
    once = r.redact("token gho_realGitToken1234567890abcdef ip 10.0.0.5")
    twice = r.redact(once)
    assert once == twice  # 对已脱敏文本再次脱敏无副作用


def test_chinese_label_credential_masked():
    """中文标签 + 空格/冒号分隔的凭据应被遮蔽（不在配置中的未知值）"""
    r = _make()
    out = r.redact("数据库密码 Db@Passw0rd2024，账号：admin_root01")
    assert "Db@Passw0rd2024" not in out
    assert "admin_root01" not in out
    assert "【已隐藏】" in out


def test_chinese_narrative_not_over_masked():
    """纯中文叙述不含 ASCII 凭据值时不应误伤"""
    r = _make()
    for text in ["密码错误，请重试", "密码是多少？", "用户名不能为空", "token 已过期"]:
        assert r.redact(text) == text


def test_chinese_label_idempotent():
    """中文标签规则对已脱敏文本幂等，不产生嵌套占位符"""
    r = _make()
    once = r.redact("密码 Db@Passw0rd2024 账号 admin_root01")
    twice = r.redact(once)
    assert once == twice
    assert "【已隐藏】【已隐藏】" not in once
