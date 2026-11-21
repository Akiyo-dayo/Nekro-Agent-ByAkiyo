"""跨模型共用的自定义字段

目前只有 `EncryptedTextField`：它原本长在 `models/db_model_group.py` 里（模型组的 `api_key`
第一个用到静态加密）。邮箱账户的授权码是同一类东西——库 dump 落在别人手里时不该是明文——
而两张表不该为了一个字段互相 import，所以字段本身挪到这里，两边都从这里取。

`db_model_group` 保留同名导入，`from nekro_agent.models.db_model_group import
EncryptedTextField` 这类既有写法照旧可用，类名、构造参数与行为一个字都没变。
"""

from typing import Any, Union

from tortoise import fields
from tortoise.models import Model

from nekro_agent.tools.credential_cipher import decrypt_credential, encrypt_credential


class EncryptedTextField(fields.TextField):
    """静态加密的文本字段

    读写都在这里收口：`to_db_value` 出去时加密、`to_python_value` 回来时解密，
    模型属性因此永远是明文，上层代码不需要知道这一列是加密的。

    ⚠️ **不要按这一列做查询过滤**（`filter(api_key=...)`）。每次加密都用新的随机 nonce，
    同一个明文两次写入得到两个不同的密文，等值匹配永远不成立 —— 而它会静默地返回空集，
    不会报错。目前全仓没有这样的调用点（凭据本来也没有按值检索的用途）。

    ⚠️ 建表 DDL 仍是 `TEXT`，列名也没变：**列名不承诺明文**，而值自己带
    `nkenc1:` 前缀说明自己是密文。刻意不新造一个叫 `xxx_encrypted` 的列，
    `secret_payload_encrypted` 就是前车之鉴（名字写着加密、存的是明文）。
    """

    def to_db_value(self, value: Any, instance: "Union[type[Model], Model]") -> Any:
        value = super().to_db_value(value, instance)
        if value is None:
            return None
        return encrypt_credential(str(value))

    def to_python_value(self, value: Any) -> Any:
        value = super().to_python_value(value)
        if value is None:
            return None
        return decrypt_credential(str(value))
