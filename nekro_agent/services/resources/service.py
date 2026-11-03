"""工作区资源的读写服务

## `secret=True` 的字段值只进不出

`DBWorkspaceResource.secret_payload_encrypted` 里装的是 SSH 私钥、root 口令、
数据库密码、访问令牌（清单见 `registry.py`）。它们的**唯一**用途是
`resolve_workspace_resources_to_env()` 把它们注入容器环境变量 —— 那条路直接读库解密，
不经过任何响应模型。

因此 `_build_detail()` 一律把 `secret=True` 字段的取值换成 `CREDENTIAL_MASK`：
详情、创建、更新三个响应模型共用它，所以三条出口一次性都关上了。
非空才换掩码、空值仍是空串，与 `routers/config.py:mask_credentials` 同一条语义
（"还没配"和"配了但看不到"必须能分辨，且写回空串不会被误判成哨兵）。

代价如实登记：**平台方也读不回自己存进去的私钥了**。这与模型组那套
"平台方看明文、租户看掩码"不同，是有意的——这两条端点整族已经是 `Role.Super`，
按身份分支等于写一条永远走不到的租户分支；而 SSH 私钥出现在一次 HTTP 响应体里，
就意味着它会进浏览器缓存、devtools、抓包与工单截图。
编辑不受影响：掩码原样提交回来表示"保持不变"（`_restore_masked_secrets`）。

**这一层关的是 HTTP 出口，不是"凭据从此受保护"。** 字段名叫 `secret_payload_encrypted`，
但 `crypto.encrypt_secret_payload` 现在就是一句 `json.dumps` —— 落库是**明文 JSON**
（`SecretPayloadFormat.PLAIN`，`secret-health` 里那个 `plaintext_count` 数的就是它；
`recover_legacy_secret_payloads` 的作用正是把旧的 AES 载荷转成这个形态，前端文案也是这么写的）。
所以 `pg_dump` / `/deployment/backup` / 直连数据库这三条路照旧拿得到原文。
读这段代码时不要把掩码当成加密。
"""

import re
from collections import Counter
from typing import Iterable

from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_workspace_resource import DBWorkspaceResource
from nekro_agent.models.db_workspace_resource_binding import DBWorkspaceResourceBinding
from nekro_agent.schemas.errors import ConflictError, NotFoundError, ValidationError
from nekro_agent.schemas.tenant import CREDENTIAL_MASK
from nekro_agent.schemas.workspace_resource import (
    BoundWorkspaceInfo,
    OrphanBindingReason,
    ResourceField,
    ResourceTemplate,
    WorkspaceResourceBinding,
    WorkspaceResourceBindingHealthResponse,
    WorkspaceResourceConflict,
    WorkspaceResourceCreate,
    WorkspaceResourceDetail,
    WorkspaceResourceOrphanBinding,
    WorkspaceResourceSecretHealthResponse,
    WorkspaceResourceSecretIssue,
    WorkspaceResourceSecretRecoverResponse,
    WorkspaceResourceSummary,
    WorkspaceResourceUpdate,
)
from nekro_agent.services.resources.crypto import (
    SecretPayloadDecodeError,
    SecretPayloadFormat,
    decode_secret_payload,
    decrypt_secret_payload,
    encrypt_secret_payload,
)
from nekro_agent.services.resources.registry import get_resource_template
from nekro_agent.tools.redaction import is_credential_mask


class WorkspaceResourceService:
    _RESOURCE_KEY_RE = re.compile(r"[^a-z0-9]+")
    _ENV_NAME_RE = re.compile(r"[^A-Z0-9]+")

    def _slugify(self, value: str, *, upper: bool = False) -> str:
        normalized = value.strip().lower()
        normalized = self._RESOURCE_KEY_RE.sub("-", normalized).strip("-")
        if not normalized:
            normalized = "resource"
        if upper:
            env = self._ENV_NAME_RE.sub("_", normalized.upper()).strip("_")
            return env or "RESOURCE"
        return normalized

    def _ensure_unique_resource_key(self, name: str, existing_keys: Iterable[str]) -> str:
        base = self._slugify(name)
        existing = set(existing_keys)
        if base not in existing:
            return base
        idx = 2
        while f"{base}-{idx}" in existing:
            idx += 1
        return f"{base}-{idx}"

    def _normalize_field(self, field: ResourceField, index: int, existing_keys: set[str]) -> ResourceField:
        field = field.model_copy(deep=True)
        field.label = field.label.strip()
        if not field.label:
            raise ValidationError(reason="资源字段名称不能为空")
        if not field.field_key:
            field.field_key = self._slugify(field.label).replace("-", "_")
        field.field_key = self._slugify(field.field_key).replace("-", "_")
        if not field.field_key:
            field.field_key = f"field_{index + 1}"
        original_key = field.field_key
        suffix = 2
        while field.field_key in existing_keys:
            field.field_key = f"{original_key}_{suffix}"
            suffix += 1
        existing_keys.add(field.field_key)
        field.order = index * 10 + 10 if field.order <= 0 else field.order
        field.fixed_aliases = [alias.strip().upper() for alias in field.fixed_aliases if alias.strip()]
        return field

    def _sorted_fields(self, fields: list[ResourceField]) -> list[ResourceField]:
        return sorted(fields, key=lambda item: (item.order, item.label))

    def _split_payload(self, fields: list[ResourceField]) -> tuple[dict[str, str], dict[str, str]]:
        public_payload: dict[str, str] = {}
        secret_payload: dict[str, str] = {}
        for field in fields:
            if field.value:
                if field.secret:
                    secret_payload[field.field_key] = field.value
                else:
                    public_payload[field.field_key] = field.value
        return public_payload, secret_payload

    def _merge_field_values(
        self, fields: list[ResourceField], public_payload: dict[str, str], secret_payload: dict[str, str]
    ) -> list[ResourceField]:
        result: list[ResourceField] = []
        for field in self._sorted_fields(fields):
            field = field.model_copy(deep=True)
            if field.secret:
                field.value = secret_payload.get(field.field_key, "")
            else:
                field.value = public_payload.get(field.field_key, "")
            result.append(field)
        return result

    def _mask_secret_fields(self, fields: list[ResourceField]) -> list[ResourceField]:
        """把 `secret=True` 字段的取值换成对外掩码

        只换非空值：空值保持空串，否则"这一项还没填"与"填了但看不到"在前端无法分辨，
        而资源中心正是靠这个差别提示运维还缺哪几项。语义与
        `routers/config.py:mask_credentials` 逐字一致。
        """
        return [
            field.model_copy(update={"value": CREDENTIAL_MASK}) if field.secret and field.value else field
            for field in fields
        ]

    def _restore_masked_secrets(
        self, fields: list[ResourceField], current_secret_payload: dict[str, str]
    ) -> list[ResourceField]:
        """把提交上来的掩码哨兵还原成库里的现值

        前端把读接口拿到的整份表单原样提交回来是常态（`ResourcesTab` 就是这么写的），
        不认哨兵的话一次无意的保存就会把 SSH 私钥覆盖成一串星号，而且
        `secret-health` 检查不出来——那串星号是一份格式完全合法的"凭据"。

        **判据是"库里这个 `field_key` 当前是不是一份被掩码掩掉的值"，不是提交上来的
        `field.secret`。** 编辑框里那个"敏感信息"是一个可以随手拨的开关
        （`ResourcesTab.tsx` 的 `Switch`），而读接口给的值只跟**库里**的 secret 标志有关。
        按提交值判的话，"把开关关掉再保存"这一步会：真私钥被丢弃 -> 字面量 `********`
        落进 `public_payload` -> 注入容器、印进 CLAUDE.md、`secret-health` 报一切正常、
        详情页看上去还是那 8 个圆点 —— 一次**完全没有信号**的凭据损毁，
        而且造出来的正是本函数要防的那份假凭据。掩码是出口上的事，
        不该改变"改这个开关"本来的语义（掩码之前那一步是把明文挪进 `public_payload`）。

        **哨兵在库里找不到对应现值时按清空处理**（新建时的 `current_secret_payload`
        是空字典，字段 key 改名、以及旧载荷解不开的资源也走这一支）。
        三种取值里只有"清空"不会凭空造出一份假凭据，而且清空在前端立刻显示成"未填写"，
        是有信号的失败。这一支仍然只对 `secret` 字段生效：一个从来就不敏感的字段里
        写着 8 个星号时，那就是用户真的想存的字符串，读接口也从没掩过它。
        """
        restored: list[ResourceField] = []
        for field in fields:
            if is_credential_mask(field.value):
                if field.field_key in current_secret_payload:
                    field = field.model_copy(update={"value": current_secret_payload[field.field_key]})
                elif field.secret:
                    field = field.model_copy(update={"value": ""})
            restored.append(field)
        return restored

    def _current_secret_payload(self, resource: DBWorkspaceResource) -> dict[str, str]:
        """读出库里现存的敏感字段，读不出来时按空处理

        捕 `ValueError` 而不是 `SecretPayloadDecodeError`：`decode_secret_payload` 里
        那句 `json.loads` **在 try 之外**，载荷压根不是 JSON 时抛的是 `json.JSONDecodeError`
        （同为 `ValueError` 子类，但不是 `SecretPayloadDecodeError`）。只捕后者的话，
        一条坏载荷会让详情与更新两条端点一起 500，而 `/resources/secret-health`
        与 `/resources/secret-purge` 正是为这种载荷准备的——用户得先能打开这个页面。
        """
        try:
            return decrypt_secret_payload(resource.secret_payload_encrypted)
        except ValueError:
            return {}

    async def _build_summary(self, resource: DBWorkspaceResource) -> WorkspaceResourceSummary:
        fields = [ResourceField.model_validate(item) for item in (resource.schema_json or [])]
        bindings = await DBWorkspaceResourceBinding.filter(resource_id=resource.id).all()
        workspace_ids = [item.workspace_id for item in bindings]
        workspaces = await DBWorkspace.filter(id__in=workspace_ids).all() if workspace_ids else []
        workspace_map = {item.id: item for item in workspaces}
        live_workspace_ids = [workspace_id for workspace_id in workspace_ids if workspace_id in workspace_map]
        fixed_aliases = sorted({alias for field in fields for alias in field.fixed_aliases})
        return WorkspaceResourceSummary(
            id=resource.id,
            resource_key=resource.resource_key,
            name=resource.name,
            template_key=resource.template_key,
            resource_note=resource.resource_note,
            resource_tags=list(resource.resource_tags_json or []),
            resource_prompt=resource.resource_prompt,
            field_count=len(fields),
            fixed_aliases=fixed_aliases,
            enabled=resource.enabled,
            # 数在世的那些，不是 `len(bindings)`：绑定表没有外键，工作区删掉之后
            # 那一行还在。按行数报会写出"已绑定 1 个工作区"配一张空清单的界面。
            bound_workspace_count=len(live_workspace_ids),
            bound_workspaces=[
                BoundWorkspaceInfo(id=workspace_id, name=workspace_map[workspace_id].name)
                for workspace_id in live_workspace_ids
            ],
            orphan_binding_count=len(bindings) - len(live_workspace_ids),
            create_time=resource.create_time.isoformat(),
            update_time=resource.update_time.isoformat(),
        )

    async def _build_detail(self, resource: DBWorkspaceResource) -> WorkspaceResourceDetail:
        """详情视图 —— 这是 `secret=True` 取值对外的**唯一**收口

        详情、创建、更新三条端点的响应模型都是它，掩码收在这里而不是各端点里：
        新增一条返回 `WorkspaceResourceDetail` 的端点会自动带上掩码，
        与 `routers/workspace_kb.py` 把守卫收进两个取目标函数是同一种做法。
        """
        summary = await self._build_summary(resource)
        fields = [ResourceField.model_validate(item) for item in (resource.schema_json or [])]
        merged_fields = self._merge_field_values(
            fields, dict(resource.public_payload or {}), self._current_secret_payload(resource)
        )
        return WorkspaceResourceDetail(**summary.model_dump(), fields=self._mask_secret_fields(merged_fields))

    async def list_resources(self) -> list[WorkspaceResourceSummary]:
        resources = await DBWorkspaceResource.all().order_by("name", "id")
        return [await self._build_summary(item) for item in resources]

    async def get_resource_or_404(self, resource_id: int) -> DBWorkspaceResource:
        resource = await DBWorkspaceResource.get_or_none(id=resource_id)
        if resource is None:
            raise NotFoundError(resource=f"工作区资源 {resource_id}")
        return resource

    async def get_resource_detail(self, resource_id: int) -> WorkspaceResourceDetail:
        resource = await self.get_resource_or_404(resource_id)
        return await self._build_detail(resource)

    def build_from_template(self, template: ResourceTemplate) -> WorkspaceResourceCreate:
        return WorkspaceResourceCreate(
            name=template.name,
            template_key=template.key,
            resource_note=template.resource_note,
            resource_tags=list(template.resource_tags),
            resource_prompt=template.resource_prompt,
            fields=[field.model_copy(deep=True) for field in template.fields],
            enabled=True,
        )

    async def create_resource(self, body: WorkspaceResourceCreate) -> WorkspaceResourceDetail:
        existing_keys = await DBWorkspaceResource.all().values_list("resource_key", flat=True)
        resource_key = self._ensure_unique_resource_key(body.name, existing_keys)
        fields = self._prepare_fields(body.fields, template_key=body.template_key)
        # 新建没有"现值"可保持，哨兵一律落成空串；不还原的话库里会存进一份字面量 `********`
        fields = self._restore_masked_secrets(fields, {})
        public_payload, secret_payload = self._split_payload(fields)
        resource = await DBWorkspaceResource.create(
            resource_key=resource_key,
            name=body.name.strip(),
            template_key=body.template_key,
            resource_note=body.resource_note.strip(),
            resource_tags_json=body.resource_tags,
            resource_prompt=body.resource_prompt.strip(),
            schema_json=[field.model_dump(exclude={"value"}) for field in fields],
            public_payload=public_payload,
            secret_payload_encrypted=encrypt_secret_payload(secret_payload),
            enabled=body.enabled,
        )
        return await self._build_detail(resource)

    async def update_resource(self, resource_id: int, body: WorkspaceResourceUpdate) -> WorkspaceResourceDetail:
        resource = await self.get_resource_or_404(resource_id)
        fields = self._prepare_fields(body.fields, template_key=body.template_key)
        # 还原必须排在 `_prepare_fields` **之后**：库里的敏感载荷按规范化后的 `field_key` 存，
        # 规范化之前拿到的 key 可能还是空的（前端新增字段时不填），对不上就会误判成清空
        fields = self._restore_masked_secrets(fields, self._current_secret_payload(resource))
        public_payload, secret_payload = self._split_payload(fields)
        resource.name = body.name.strip()
        resource.template_key = body.template_key
        resource.resource_note = body.resource_note.strip()
        resource.resource_tags_json = body.resource_tags
        resource.resource_prompt = body.resource_prompt.strip()
        resource.schema_json = [field.model_dump(exclude={"value"}) for field in fields]
        resource.public_payload = public_payload
        resource.secret_payload_encrypted = encrypt_secret_payload(secret_payload)
        resource.enabled = body.enabled
        await resource.save()
        await self.refresh_related_workspace_caches(resource.id)
        return await self._build_detail(resource)

    async def delete_resource(self, resource_id: int, *, remove_bindings: bool = False) -> None:
        """删除资源

        **指向已不存在工作区的绑定不算引用。** 改前这里数的是 `len(bindings)`，
        于是「删工作区 -> 绑定悬空 -> 这条资源从此 400，永远删不掉」；
        当时唯一的出路是直连数据库。这一支同时是**存量脏数据的自愈路径**：
        用户点一次"删除资源"就把它自己名下那几行悬空绑定带走了，不需要任何一次性脚本，
        也不需要 `remove_bindings=true`（那一档的语义是"我知道有工作区在用，照删"，
        而悬空绑定压根不是"有工作区在用"）。
        """
        resource = await self.get_resource_or_404(resource_id)
        bindings = await DBWorkspaceResourceBinding.filter(resource_id=resource_id).all()
        live_workspace_ids = await self._live_workspace_ids(binding.workspace_id for binding in bindings)
        live_bindings = [binding for binding in bindings if binding.workspace_id in live_workspace_ids]
        if live_bindings and not remove_bindings:
            raise ValidationError(reason=f"该资源仍被 {len(live_bindings)} 个工作区引用，无法删除")
        for binding in bindings:
            await binding.delete()
        await resource.delete()
        for workspace_id in sorted({binding.workspace_id for binding in live_bindings}):
            await self.refresh_workspace_cache(workspace_id)

    async def _live_workspace_ids(self, workspace_ids: Iterable[int]) -> set[int]:
        """这批 ID 里真实存在的工作区

        `workspace_resource_binding.workspace_id` 是普通 `IntField` 而不是外键
        （本项目所有 `workspace_id` 列都是这个形态），数据库不会替我们挡掉悬空行，
        所以每个消费方都得自己问一句「那个工作区还在吗」。判据只有一处，
        `_build_summary` / `delete_resource` / `check_binding_health` 共用它。
        """
        ids = {int(workspace_id) for workspace_id in workspace_ids}
        if not ids:
            return set()
        rows = await DBWorkspace.filter(id__in=sorted(ids)).values_list("id", flat=True)
        return {int(row) for row in rows}

    async def purge_workspace_bindings(self, workspace_id: int) -> int:
        """工作区被删除时解除它的全部资源绑定

        级联的收口：绑定表没有外键，工作区行一删这些绑定就成了既看不见、
        又让对应资源永远删不掉的悬空行。放在 service 里而不是路由里，
        是因为判据（"绑定属于哪个工作区"）只该有一处定义。

        Returns:
            解除的绑定条数
        """
        return await DBWorkspaceResourceBinding.filter(workspace_id=workspace_id).delete()

    async def check_binding_health(self) -> WorkspaceResourceBindingHealthResponse:
        """列出全部悬空绑定 —— 后台上唯一能看见它们的地方

        与 `check_secret_payload_health` 同一形态（一条只读的体检 + 一条显式的清理动作）。
        **不做启动时自愈**：这张表是"谁把哪份凭据交给了哪个工作区"的唯一记录，
        自动删等于让一次误判无声地抹掉证据；而且判据要读 `workspace` 表，
        迁移未完成 / 连接刚起来这类时刻拿到的"工作区不存在"是假的。
        所以这里只报告，删由用户按一次按钮（或者删那条资源时顺带带走）。
        """
        bindings = await DBWorkspaceResourceBinding.all().order_by("id")
        if not bindings:
            return WorkspaceResourceBindingHealthResponse(ok=True)

        live_workspace_ids = await self._live_workspace_ids(binding.workspace_id for binding in bindings)
        resource_ids = sorted({binding.resource_id for binding in bindings})
        resources = await DBWorkspaceResource.filter(id__in=resource_ids).all()
        resource_map = {item.id: item for item in resources}

        orphans: list[WorkspaceResourceOrphanBinding] = []
        for binding in bindings:
            workspace_missing = binding.workspace_id not in live_workspace_ids
            resource_missing = binding.resource_id not in resource_map
            if not workspace_missing and not resource_missing:
                continue
            reason: OrphanBindingReason = (
                "both_missing"
                if workspace_missing and resource_missing
                else ("workspace_missing" if workspace_missing else "resource_missing")
            )
            resource = resource_map.get(binding.resource_id)
            orphans.append(
                WorkspaceResourceOrphanBinding(
                    binding_id=binding.id,
                    workspace_id=binding.workspace_id,
                    resource_id=binding.resource_id,
                    resource_name=resource.name if resource else "",
                    reason=reason,
                    note=binding.note,
                    create_time=binding.create_time.isoformat(),
                )
            )

        return WorkspaceResourceBindingHealthResponse(
            ok=not orphans,
            total=len(bindings),
            live_count=len(bindings) - len(orphans),
            orphan_count=len(orphans),
            orphans=orphans,
        )

    async def purge_orphan_bindings(self) -> int:
        """删掉全部悬空绑定，按 `binding_id` 精确删

        不用 `exclude(workspace_id__in=...)` 之类的整表条件删：判据与体检必须是同一处，
        否则"体检报了 3 条、清理删了 5 条"这种事没有任何信号。
        """
        health = await self.check_binding_health()
        if not health.orphans:
            return 0
        binding_ids = [item.binding_id for item in health.orphans]
        await DBWorkspaceResourceBinding.filter(id__in=binding_ids).delete()
        # 工作区还在、资源没了的那一种：删完要刷新该工作区的提示词缓存与计数
        affected = {item.workspace_id for item in health.orphans if item.reason == "resource_missing"}
        for workspace_id in sorted(affected):
            await self.refresh_workspace_cache(workspace_id)
        return len(binding_ids)

    def _prepare_fields(self, fields: list[ResourceField], *, template_key: str | None) -> list[ResourceField]:
        if not fields and template_key:
            template = get_resource_template(template_key)
            if template is not None:
                fields = [field.model_copy(deep=True) for field in template.fields]
        if not fields:
            raise ValidationError(reason="资源至少需要一个字段")
        existing_keys: set[str] = set()
        normalized = [
            self._normalize_field(field, index, existing_keys)
            for index, field in enumerate(self._sorted_fields(fields))
        ]
        return normalized

    def _build_env_name(self, resource_key: str, field_key: str) -> str:
        return f"NEKRO_RESOURCE_{self._slugify(resource_key, upper=True)}_{self._slugify(field_key, upper=True)}"

    async def check_secret_payload_health(self) -> WorkspaceResourceSecretHealthResponse:
        resources = await DBWorkspaceResource.all().order_by("name", "id")
        plaintext_count = 0
        legacy_encrypted_count = 0
        invalid_count = 0
        issues: list[WorkspaceResourceSecretIssue] = []

        for resource in resources:
            try:
                result = decode_secret_payload(resource.secret_payload_encrypted)
            except Exception as e:
                invalid_count += 1
                issues.append(
                    WorkspaceResourceSecretIssue(
                        resource_id=resource.id,
                        resource_name=resource.name,
                        reason=str(e),
                    )
                )
                continue
            if result.format in (SecretPayloadFormat.EMPTY, SecretPayloadFormat.PLAIN):
                plaintext_count += 1
            elif result.format == SecretPayloadFormat.LEGACY_ENCRYPTED:
                legacy_encrypted_count += 1

        return WorkspaceResourceSecretHealthResponse(
            ok=invalid_count == 0,
            total=len(resources),
            plaintext_count=plaintext_count,
            legacy_encrypted_count=legacy_encrypted_count,
            invalid_count=invalid_count,
            issues=issues,
        )

    async def recover_legacy_secret_payloads(self, legacy_secret: str) -> WorkspaceResourceSecretRecoverResponse:
        resources = await DBWorkspaceResource.all().order_by("name", "id")
        recovered_count = 0
        issues: list[WorkspaceResourceSecretIssue] = []

        for resource in resources:
            try:
                result = decode_secret_payload(resource.secret_payload_encrypted)
            except SecretPayloadDecodeError:
                try:
                    result = decode_secret_payload(resource.secret_payload_encrypted, legacy_secret=legacy_secret)
                except Exception as e:
                    issues.append(
                        WorkspaceResourceSecretIssue(
                            resource_id=resource.id,
                            resource_name=resource.name,
                            reason=str(e),
                        )
                    )
                    continue
            except Exception as e:
                issues.append(
                    WorkspaceResourceSecretIssue(
                        resource_id=resource.id,
                        resource_name=resource.name,
                        reason=str(e),
                    )
                )
                continue
            if result.format == SecretPayloadFormat.LEGACY_ENCRYPTED:
                resource.secret_payload_encrypted = encrypt_secret_payload(result.payload)
                await resource.save(update_fields=["secret_payload_encrypted", "update_time"])
                recovered_count += 1

        return WorkspaceResourceSecretRecoverResponse(
            ok=not issues,
            recovered_count=recovered_count,
            invalid_count=len(issues),
            issues=issues,
        )

    async def purge_unreadable_secret_payloads(self) -> int:
        resources = await DBWorkspaceResource.all().order_by("name", "id")
        purged_count = 0
        for resource in resources:
            try:
                decode_secret_payload(resource.secret_payload_encrypted)
            except Exception:
                resource.secret_payload_encrypted = ""
                await resource.save(update_fields=["secret_payload_encrypted", "update_time"])
                purged_count += 1
        return purged_count

    async def check_bind_conflicts(self, workspace_id: int, resource_id: int) -> list[WorkspaceResourceConflict]:
        target = await self.get_resource_or_404(resource_id)
        target_fields = [ResourceField.model_validate(item) for item in (target.schema_json or [])]
        target_aliases = {alias for field in target_fields for alias in field.fixed_aliases}
        if not target_aliases:
            return []
        bindings = await DBWorkspaceResourceBinding.filter(workspace_id=workspace_id, enabled=True).all()
        if not bindings:
            return []
        resource_ids = [item.resource_id for item in bindings if item.resource_id != resource_id]
        existing_resources = await DBWorkspaceResource.filter(id__in=resource_ids, enabled=True).all()
        conflicts: list[WorkspaceResourceConflict] = []
        for resource in existing_resources:
            fields = [ResourceField.model_validate(item) for item in (resource.schema_json or [])]
            aliases = {alias for field in fields for alias in field.fixed_aliases}
            for alias in sorted(target_aliases & aliases):
                conflicts.append(
                    WorkspaceResourceConflict(
                        env_name=alias,
                        existing_resource_id=resource.id,
                        existing_resource_name=resource.name,
                        target_resource_id=target.id,
                        target_resource_name=target.name,
                    )
                )
        return conflicts

    async def bind_resource(self, workspace_id: int, resource_id: int, *, note: str = "") -> WorkspaceResourceBinding:
        workspace = await DBWorkspace.get_or_none(id=workspace_id)
        if workspace is None:
            raise NotFoundError(resource=f"工作区 {workspace_id}")
        resource = await self.get_resource_or_404(resource_id)
        conflicts = await self.check_bind_conflicts(workspace_id, resource_id)
        if conflicts:
            first = conflicts[0]
            raise ValidationError(
                reason=(
                    f"注入环境变量 {first.env_name} 与资源“{first.existing_resource_name}”冲突，"
                    "请不要在同一工作区绑定这两条资源"
                )
            )
        existing = await DBWorkspaceResourceBinding.get_or_none(workspace_id=workspace_id, resource_id=resource_id)
        if existing is not None:
            raise ConflictError(resource=f"工作区资源绑定 {workspace_id}/{resource_id}")
        current_max = await DBWorkspaceResourceBinding.filter(workspace_id=workspace_id).order_by("-sort_order").first()
        sort_order = (current_max.sort_order if current_max else 0) + 10
        binding = await DBWorkspaceResourceBinding.create(
            workspace_id=workspace_id,
            resource_id=resource_id,
            enabled=resource.enabled,
            sort_order=sort_order,
            note=note.strip(),
        )
        await self.refresh_workspace_cache(workspace_id)
        return await self._build_binding(binding, resource=resource)

    async def _build_binding(
        self, binding: DBWorkspaceResourceBinding, *, resource: DBWorkspaceResource | None = None
    ) -> WorkspaceResourceBinding:
        resource = resource or await self.get_resource_or_404(binding.resource_id)
        return WorkspaceResourceBinding(
            binding_id=binding.id,
            resource_id=resource.id,
            enabled=binding.enabled,
            sort_order=binding.sort_order,
            note=binding.note,
            resource=await self._build_summary(resource),
        )

    async def list_workspace_bindings(self, workspace_id: int) -> list[WorkspaceResourceBinding]:
        bindings = await DBWorkspaceResourceBinding.filter(workspace_id=workspace_id).order_by("sort_order", "id")
        if not bindings:
            return []
        resources = await DBWorkspaceResource.filter(id__in=[item.resource_id for item in bindings]).all()
        resource_map = {item.id: item for item in resources}
        return [
            await self._build_binding(binding, resource=resource_map[binding.resource_id])
            for binding in bindings
            if binding.resource_id in resource_map
        ]

    async def unbind_resource(self, workspace_id: int, resource_id: int) -> None:
        binding = await DBWorkspaceResourceBinding.get_or_none(workspace_id=workspace_id, resource_id=resource_id)
        if binding is None:
            raise NotFoundError(resource=f"工作区资源绑定 {workspace_id}/{resource_id}")
        await binding.delete()
        await self.refresh_workspace_cache(workspace_id)

    async def reorder_bindings(self, workspace_id: int, binding_ids: list[int]) -> None:
        bindings = await DBWorkspaceResourceBinding.filter(workspace_id=workspace_id, id__in=binding_ids).all()
        binding_map = {item.id: item for item in bindings}
        if len(binding_map) != len(binding_ids):
            raise ValidationError(reason="资源排序数据不完整")
        for index, binding_id in enumerate(binding_ids, start=1):
            binding = binding_map[binding_id]
            binding.sort_order = index * 10
            await binding.save(update_fields=["sort_order", "update_time"])
        await self.refresh_workspace_cache(workspace_id)

    async def resolve_workspace_resources_to_env(self, workspace_id: int) -> dict[str, str]:
        bindings = await DBWorkspaceResourceBinding.filter(workspace_id=workspace_id, enabled=True).order_by(
            "sort_order", "id"
        )
        if not bindings:
            return {}
        resources = await DBWorkspaceResource.filter(id__in=[item.resource_id for item in bindings], enabled=True).all()
        resource_map = {item.id: item for item in resources}
        env_vars: dict[str, str] = {}
        alias_counter: Counter[str] = Counter()
        for binding in bindings:
            resource = resource_map.get(binding.resource_id)
            if resource is None:
                continue
            fields = [ResourceField.model_validate(item) for item in (resource.schema_json or [])]
            try:
                secret_payload = decrypt_secret_payload(resource.secret_payload_encrypted)
            except SecretPayloadDecodeError as e:
                raise ValidationError(reason=f"资源“{resource.name}”的敏感字段不可用，请在资源中心恢复或重新填写") from e
            public_payload = dict(resource.public_payload or {})
            for field in fields:
                if field.export_mode != "env":
                    continue
                value = (
                    secret_payload.get(field.field_key, "") if field.secret else public_payload.get(field.field_key, "")
                )
                if not value:
                    continue
                env_name = self._build_env_name(resource.resource_key, field.field_key)
                env_vars[env_name] = value
                for alias in field.fixed_aliases:
                    alias_counter[alias] += 1
                    if alias in env_vars and env_vars[alias] != value:
                        raise ValidationError(reason=f"工作区资源注入环境变量 {alias} 冲突")
                    env_vars[alias] = value
        duplicated = [alias for alias, count in alias_counter.items() if count > 1]
        if duplicated:
            raise ValidationError(reason=f"工作区资源注入环境变量冲突: {', '.join(sorted(duplicated))}")
        return env_vars

    async def _build_workspace_prompt_section(self, workspace_id: int) -> tuple[str, str]:
        bindings = await self.list_workspace_bindings(workspace_id)
        if not bindings:
            return "", "（当前工作区未绑定任何工作区资源）"
        summary_lines = []
        prompt_lines = ["## 可用工作区资源", ""]
        for item in bindings:
            resource = item.resource
            summary_lines.append(f"- {resource.name}: {resource.resource_note or '已挂载工作区资源'}")
            detail = await self.get_resource_detail(resource.id)
            prompt_lines.append(f"### 资源：{detail.name}")
            for field in detail.fields:
                if field.export_mode != "env":
                    continue
                env_name = self._build_env_name(detail.resource_key, field.field_key)
                env_names = [*field.fixed_aliases, env_name] if field.fixed_aliases else [env_name]
                env_text = "、".join(f"`{name}`" for name in env_names)
                if field.secret:
                    prompt_lines.append(f"- {field.label}：已注入环境变量 {env_text}")
                else:
                    value_text = f"`{field.value}`" if field.value else "（未填写）"
                    prompt_lines.append(f"- {field.label}：{value_text}")
                    prompt_lines.append(f"  已注入环境变量：{env_text}")
            if detail.resource_prompt:
                prompt_lines.append(f"- 使用方式：{detail.resource_prompt}")
            prompt_lines.append("")
        return "\n".join(prompt_lines).strip(), "\n".join(summary_lines)

    async def refresh_workspace_cache(self, workspace_id: int) -> None:
        workspace = await DBWorkspace.get_or_none(id=workspace_id)
        if workspace is None:
            return
        prompt_text, summary_text = await self._build_workspace_prompt_section(workspace_id)
        metadata = dict(workspace.metadata or {})
        metadata["resource_prompt_cache"] = prompt_text
        metadata["resource_summary_cache"] = summary_text
        metadata["resource_count_cache"] = await DBWorkspaceResourceBinding.filter(workspace_id=workspace_id).count()
        workspace.metadata = metadata
        await workspace.save(update_fields=["metadata", "update_time"])
        from nekro_agent.services.workspace.manager import WorkspaceService

        WorkspaceService.update_claude_md(workspace)

    async def refresh_related_workspace_caches(self, resource_id: int) -> None:
        workspace_ids = await DBWorkspaceResourceBinding.filter(resource_id=resource_id).values_list(
            "workspace_id", flat=True
        )
        for workspace_id in workspace_ids:
            await self.refresh_workspace_cache(int(workspace_id))


workspace_resource_service = WorkspaceResourceService()
