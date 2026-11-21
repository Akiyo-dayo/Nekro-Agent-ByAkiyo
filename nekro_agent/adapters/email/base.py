"""邮箱适配器基础类和提供商接口"""

from typing import Any, Dict, List, Optional

# 邮箱提供商配置映射
#
# 键就是账户上的 `EMAIL_ACCOUNT` 取值，也是实例自有账户表里 `provider` 列的取值；页面的
# 下拉直接按这份映射渲染。`note` 是页面上照写的那句话：怎么开通服务、该填登录密码还是授权码。
# 主机与端口来自各服务商的公开文档，没有逐个实测过（见交付报告）。
EMAIL_PROVIDER_CONFIGS: Dict[str, Dict[str, str]] = {
    "QQ邮箱": {
        "imap_host": "imap.qq.com",
        "imap_port": "993",
        "smtp_host": "smtp.qq.com",
        "smtp_port": "587",
        "smtp_ssl_port": "465",
        "smtp_use_ssl": "true",  # QQ 邮箱推荐使用 SSL
        "note": "在设置 → 账户里开启服务并生成授权码，密码栏填授权码",
    },
    "163邮箱": {
        "imap_host": "imap.163.com",
        "imap_port": "993",
        "smtp_host": "smtp.163.com",
        "smtp_port": "587",
        "smtp_ssl_port": "465",
        "note": "在网页版设置里开启 IMAP/SMTP，填「授权码」，不是登录密码",
    },
    "126邮箱": {
        "imap_host": "imap.126.com",
        "imap_port": "993",
        "smtp_host": "smtp.126.com",
        "smtp_port": "587",
        "smtp_ssl_port": "465",
        "smtp_use_ssl": "true",
        "note": "在网页版设置里开启 IMAP/SMTP，填「授权码」，不是登录密码",
    },
    "腾讯企业邮": {
        "imap_host": "imap.exmail.qq.com",
        "imap_port": "993",
        "smtp_host": "smtp.exmail.qq.com",
        "smtp_port": "587",
        "smtp_ssl_port": "465",
        "smtp_use_ssl": "true",
        "note": "开了安全登录的要用「客户端专用密码」",
    },
    "阿里云邮箱（个人版）": {
        "imap_host": "imap.aliyun.com",
        "imap_port": "993",
        "smtp_host": "smtp.aliyun.com",
        "smtp_port": "587",
        "smtp_ssl_port": "465",
        "smtp_use_ssl": "true",
        "note": "在设置里开启 IMAP/SMTP",
    },
    "阿里云邮箱（企业版）": {
        "imap_host": "imap.qiye.aliyun.com",
        "imap_port": "993",
        "smtp_host": "smtp.qiye.aliyun.com",
        "smtp_port": "587",
        "smtp_ssl_port": "465",
        "smtp_use_ssl": "true",
        "note": "管理员要先允许客户端收发",
    },
    "Gmail": {
        "imap_host": "imap.gmail.com",
        "imap_port": "993",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": "587",
        "smtp_ssl_port": "465",
        "note": "要先开两步验证，再生成「应用专用密码」",
    },
    "Outlook": {
        "imap_host": "outlook.office365.com",
        "imap_port": "993",
        "smtp_host": "smtp-mail.outlook.com",
        "smtp_port": "587",
        "smtp_ssl_port": "587",
        "note": "微软在逐步关闭密码登录（基本认证），授权码方式很可能不可用；"
        "连接失败时请改用平台方提供的邮箱，或联系平台方",
    },
    "自定义": {
        "note": "主机过公网校验，端口只许 993 / 143 / 465 / 587 / 25",
    },
}

# 自定义服务器的标记值。表里 `provider` 存它时，主机端口取自账户自己的 settings
CUSTOM_PROVIDER_KEY = "自定义"


def list_provider_presets() -> List[Dict[str, Any]]:
    """服务商预设的只读列表，给后台的「实例自有邮箱」新建 / 修改表单用

    只给页面上要用的那几个字段：键、四个主机端口、要不要 SSL、那句说明。凭据、认证方式这类
    东西本来就不在预设里，也没有按账户区分的内容，所以这里不需要再掩码一遍。
    自定义那一项（`自定义`）照旧出现在列表末尾，页面上选了它才显示自填的主机与端口。
    """
    presets: List[Dict[str, Any]] = []
    for key, config in EMAIL_PROVIDER_CONFIGS.items():
        presets.append(
            {
                "key": key,
                "imap_host": config.get("imap_host", ""),
                "imap_port": int(config["imap_port"]) if config.get("imap_port") else None,
                "smtp_host": config.get("smtp_host", ""),
                "smtp_port": int(config["smtp_port"]) if config.get("smtp_port") else None,
                "smtp_ssl_port": int(config["smtp_ssl_port"]) if config.get("smtp_ssl_port") else None,
                "smtp_use_ssl": str(config.get("smtp_use_ssl", "")).lower() == "true",
                "is_custom": key == CUSTOM_PROVIDER_KEY,
                "note": config.get("note", ""),
            },
        )
    return presets


def provider_preset(key: str) -> Optional[Dict[str, str]]:
    """按预设键取配置；不认识的键返回 None（调用方按自定义处理或拒绝）"""
    return EMAIL_PROVIDER_CONFIGS.get(key)
