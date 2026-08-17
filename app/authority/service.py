import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.authority.auth import credential_digest
from app.authority.decisions import AuthorityDecisionResult, AuthorityOutcome
from app.authority.defaults import DEFAULT_POLICIES, FORBIDDEN_AI_SCOPES, SUPPORTED_SCOPES
from app.authority.evaluators import evaluate_policy
from app.authority.facts import validate_action_facts
from app.crm.auth import AuthenticatedUser
from app.database.models.authority import (
    AIPrincipalScope,
    AIServicePrincipal,
    AuthorityApprovalRequest,
    AuthorityDecision,
    AuthorityPolicy,
    AuthorityPolicyVersion,
    BusinessSettings,
    BusinessSettingVersion,
)
from app.database.models.ai_action import AIActionRequest, ApprovalDecision
from app.ai_actions.state_machine import AIActionStatus, assert_transition
from app.events.service import record_business_event
from app.idempotency.service import hash_request


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def iso(value) -> str | None:
    return value.isoformat() if value else None


def decimal_value(value, field: str) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be numeric.") from exc


class AuthorityService:
    """Tenant-scoped control plane for Jarvis authority.

    Human CRM credentials call the mutation methods. AI credentials are stored
    separately and can only be used for scoped execution checks.
    """

    def __init__(self, session_factory: async_sessionmaker):
        self.session_factory = session_factory

    @staticmethod
    def settings_payload(settings: BusinessSettings) -> dict:
        return {
            "business_id": settings.business_id,
            "ai_operating_mode": settings.ai_operating_mode,
            "currency": settings.currency,
            "timezone": settings.timezone,
            "maximum_automatic_discount_pct": float(
                settings.maximum_automatic_discount_pct
            ),
            "maximum_automatic_quotation_value": float(
                settings.maximum_automatic_quotation_value
            ),
            "minimum_margin_pct": float(settings.minimum_margin_pct),
            "daily_outbound_message_limit": settings.daily_outbound_message_limit,
            "default_approval_role": settings.default_approval_role,
            "version": settings.version,
            "created_at": iso(settings.created_at),
            "updated_at": iso(settings.updated_at),
        }

    @staticmethod
    def principal_payload(principal: AIServicePrincipal, scopes: list[str]) -> dict:
        return {
            "id": principal.id,
            "business_id": principal.business_id,
            "name": principal.name,
            "principal_type": principal.principal_type,
            "credential_prefix": principal.credential_prefix,
            "status": principal.status,
            "scopes": sorted(scopes),
            "created_at": iso(principal.created_at),
            "updated_at": iso(principal.updated_at),
            "last_used_at": iso(principal.last_used_at),
            "rotated_at": iso(principal.rotated_at),
            "revoked_at": iso(principal.revoked_at),
        }

    @staticmethod
    def policy_payload(policy: AuthorityPolicy, version: AuthorityPolicyVersion) -> dict:
        return {
            "id": policy.id,
            "business_id": policy.business_id,
            "action_type": policy.action_type,
            "name": policy.name,
            "description": policy.description,
            "enabled": policy.enabled,
            "active_version": policy.active_version,
            "decision_mode": version.decision_mode,
            "risk_level": version.risk_level,
            "required_scope": version.required_scope,
            "approval_role": version.approval_role,
            "conditions": version.conditions,
            "effective_from": iso(version.effective_from),
            "created_at": iso(version.created_at),
        }

    async def _create_defaults(self, session, user: AuthenticatedUser) -> BusinessSettings:
        settings = BusinessSettings(
            business_id=user.business_id,
            ai_operating_mode="recommend_only",
            currency="INR",
            timezone="Asia/Kolkata",
            maximum_automatic_discount_pct=Decimal("3.0"),
            maximum_automatic_quotation_value=Decimal("5000000.00"),
            minimum_margin_pct=Decimal("12.0"),
            daily_outbound_message_limit=100,
            default_approval_role="admin",
            version=1,
            created_by_user_id=user.user_id,
            updated_by_user_id=user.user_id,
        )
        session.add(settings)
        await session.flush()
        snapshot = self.settings_payload(settings)
        session.add(BusinessSettingVersion(
            business_id=user.business_id,
            version=1,
            settings_snapshot=snapshot,
            change_reason="Safe Batch 1 defaults initialized",
            created_by_user_id=user.user_id,
        ))
        for spec in DEFAULT_POLICIES:
            policy = AuthorityPolicy(
                business_id=user.business_id,
                action_type=spec["action_type"],
                name=spec["name"],
                description=f"Default authority policy for {spec['name'].lower()}.",
                active_version=1,
                enabled=True,
            )
            session.add(policy)
            await session.flush()
            session.add(AuthorityPolicyVersion(
                business_id=user.business_id,
                policy_id=policy.id,
                action_type=spec["action_type"],
                version=1,
                decision_mode=spec["decision_mode"],
                risk_level=spec["risk_level"],
                required_scope=spec["required_scope"],
                approval_role=spec["approval_role"],
                conditions=spec["conditions"],
                change_reason="Safe Batch 1 default policy",
                created_by_user_id=user.user_id,
            ))
        await record_business_event(
            session,
            business_id=user.business_id,
            event_type="ai_authority_defaults_initialized",
            source="crm",
            actor_type="human",
            actor_id=user.user_id,
            entity_type="business_settings",
            entity_id=user.business_id,
            data={"version": 1, "ai_operating_mode": "recommend_only"},
        )
        return settings

    async def ensure_defaults(self, user: AuthenticatedUser) -> BusinessSettings:
        async with self.session_factory() as session:
            settings = await session.get(BusinessSettings, user.business_id)
            if settings is None:
                settings = await self._create_defaults(session, user)
                try:
                    await session.commit()
                    await session.refresh(settings)
                except IntegrityError:
                    # Two startup/API workers may initialize the same tenant at
                    # once. The unique business key makes one the winner; the
                    # other reloads the committed control plane.
                    await session.rollback()
                    settings = await session.get(BusinessSettings, user.business_id)
                    if settings is None:
                        raise
            return settings

    async def get_settings(self, user: AuthenticatedUser) -> dict:
        return self.settings_payload(await self.ensure_defaults(user))

    async def update_settings(self, user: AuthenticatedUser, values: dict) -> dict:
        expected_version = values.pop("expected_version")
        reason = values.pop("change_reason")
        await self.ensure_defaults(user)
        async with self.session_factory() as session:
            settings = await session.scalar(
                select(BusinessSettings)
                .where(BusinessSettings.business_id == user.business_id)
                .with_for_update()
            )
            if settings.version != expected_version:
                raise HTTPException(
                    status_code=409,
                    detail=(f"Settings version is {settings.version}; "
                            f"expected {expected_version}. Refresh and retry."),
                )
            for key, value in values.items():
                setattr(settings, key, value)
            settings.version += 1
            settings.updated_by_user_id = user.user_id
            settings.updated_at = utc_now()
            await session.flush()
            payload = self.settings_payload(settings)
            session.add(BusinessSettingVersion(
                business_id=user.business_id,
                version=settings.version,
                settings_snapshot=payload,
                change_reason=reason,
                created_by_user_id=user.user_id,
            ))
            await record_business_event(
                session,
                business_id=user.business_id,
                event_type="business_ai_settings_changed",
                source="crm",
                actor_type="human",
                actor_id=user.user_id,
                entity_type="business_settings",
                entity_id=user.business_id,
                data={"version": settings.version, "reason": reason},
            )
            await session.commit()
            return payload

    async def settings_history(self, user: AuthenticatedUser) -> list[dict]:
        await self.ensure_defaults(user)
        async with self.session_factory() as session:
            rows = (await session.scalars(
                select(BusinessSettingVersion).where(
                    BusinessSettingVersion.business_id == user.business_id
                ).order_by(BusinessSettingVersion.version.desc())
            )).all()
        return [{
            "id": row.id,
            "version": row.version,
            "settings": row.settings_snapshot,
            "change_reason": row.change_reason,
            "created_by_user_id": row.created_by_user_id,
            "created_at": iso(row.created_at),
        } for row in rows]

    @staticmethod
    def _validate_scope(scope: str) -> None:
        if scope in FORBIDDEN_AI_SCOPES or scope not in SUPPORTED_SCOPES:
            raise HTTPException(status_code=422, detail=f"AI scope '{scope}' is not grantable.")

    async def list_principals(self, user: AuthenticatedUser) -> list[dict]:
        async with self.session_factory() as session:
            principals = (await session.scalars(select(AIServicePrincipal).where(
                AIServicePrincipal.business_id == user.business_id
            ).order_by(AIServicePrincipal.name))).all()
            result = []
            for principal in principals:
                scopes = (await session.scalars(select(AIPrincipalScope.scope).where(
                    AIPrincipalScope.business_id == user.business_id,
                    AIPrincipalScope.principal_id == principal.id,
                    AIPrincipalScope.revoked_at.is_(None),
                ))).all()
                result.append(self.principal_payload(principal, list(scopes)))
            return result

    async def create_principal(
        self, user: AuthenticatedUser, *, name: str, scopes: list[str]
    ) -> dict:
        unique_scopes = sorted(set(scopes))
        for scope in unique_scopes:
            self._validate_scope(scope)
        token = "jarvis_live_" + secrets.token_urlsafe(48)
        async with self.session_factory() as session:
            existing = await session.scalar(select(AIServicePrincipal.id).where(
                AIServicePrincipal.business_id == user.business_id,
                AIServicePrincipal.name == name.strip(),
            ))
            if existing:
                raise HTTPException(status_code=409, detail="An AI principal with this name exists.")
            principal = AIServicePrincipal(
                business_id=user.business_id,
                name=name.strip(),
                credential_prefix=token[:20],
                credential_hash=credential_digest(token),
                created_by_user_id=user.user_id,
            )
            session.add(principal)
            await session.flush()
            for scope in unique_scopes:
                session.add(AIPrincipalScope(
                    business_id=user.business_id,
                    principal_id=principal.id,
                    scope=scope,
                    granted_by_user_id=user.user_id,
                ))
            await record_business_event(
                session,
                business_id=user.business_id,
                event_type="ai_service_principal_created",
                source="crm",
                actor_type="human",
                actor_id=user.user_id,
                entity_type="ai_service_principal",
                entity_id=principal.id,
                data={"name": principal.name, "scopes": unique_scopes},
            )
            await session.commit()
            payload = self.principal_payload(principal, unique_scopes)
            payload["credential"] = token
            payload["credential_warning"] = "Save this credential now; it is never shown again."
            return payload

    async def rotate_principal(self, user: AuthenticatedUser, principal_id: str, reason: str) -> dict:
        token = "jarvis_live_" + secrets.token_urlsafe(48)
        async with self.session_factory() as session:
            principal = await session.scalar(select(AIServicePrincipal).where(
                AIServicePrincipal.id == principal_id,
                AIServicePrincipal.business_id == user.business_id,
            ).with_for_update())
            if principal is None:
                raise HTTPException(status_code=404, detail="AI principal was not found.")
            if principal.status != "active":
                raise HTTPException(status_code=409, detail="Only an active principal can be rotated.")
            principal.credential_hash = credential_digest(token)
            principal.credential_prefix = token[:20]
            principal.rotated_at = utc_now()
            await record_business_event(
                session, business_id=user.business_id,
                event_type="ai_service_principal_credential_rotated",
                source="crm", actor_type="human", actor_id=user.user_id,
                entity_type="ai_service_principal", entity_id=principal.id,
                data={"reason": reason},
            )
            await session.commit()
            scopes = (await session.scalars(select(AIPrincipalScope.scope).where(
                AIPrincipalScope.business_id == user.business_id,
                AIPrincipalScope.principal_id == principal.id,
                AIPrincipalScope.revoked_at.is_(None),
            ))).all()
            payload = self.principal_payload(principal, list(scopes))
            payload["credential"] = token
            payload["credential_warning"] = "Save this credential now; it is never shown again."
            return payload

    async def revoke_principal(self, user: AuthenticatedUser, principal_id: str, reason: str) -> dict:
        async with self.session_factory() as session:
            principal = await session.scalar(select(AIServicePrincipal).where(
                AIServicePrincipal.id == principal_id,
                AIServicePrincipal.business_id == user.business_id,
            ).with_for_update())
            if principal is None:
                raise HTTPException(status_code=404, detail="AI principal was not found.")
            if principal.status == "revoked":
                return self.principal_payload(principal, [])
            now = utc_now()
            principal.status = "revoked"
            principal.revoked_at = now
            active_scopes = (await session.scalars(select(AIPrincipalScope).where(
                AIPrincipalScope.business_id == user.business_id,
                AIPrincipalScope.principal_id == principal.id,
                AIPrincipalScope.revoked_at.is_(None),
            ))).all()
            for scope in active_scopes:
                scope.revoked_at = now
                scope.revoked_by_user_id = user.user_id
            await record_business_event(
                session, business_id=user.business_id,
                event_type="ai_service_principal_revoked", source="crm",
                actor_type="human", actor_id=user.user_id,
                entity_type="ai_service_principal", entity_id=principal.id,
                data={"reason": reason},
            )
            await session.commit()
            return self.principal_payload(principal, [])

    async def change_scope(
        self, user: AuthenticatedUser, principal_id: str, scope: str,
        reason: str, *, grant: bool,
    ) -> dict:
        self._validate_scope(scope)
        async with self.session_factory() as session:
            principal = await session.scalar(select(AIServicePrincipal).where(
                AIServicePrincipal.id == principal_id,
                AIServicePrincipal.business_id == user.business_id,
            ))
            if principal is None:
                raise HTTPException(status_code=404, detail="AI principal was not found.")
            if principal.status != "active":
                raise HTTPException(status_code=409, detail="AI principal is not active.")
            row = await session.scalar(select(AIPrincipalScope).where(
                AIPrincipalScope.business_id == user.business_id,
                AIPrincipalScope.principal_id == principal_id,
                AIPrincipalScope.scope == scope,
            ).with_for_update())
            now = utc_now()
            if grant:
                if row is None:
                    row = AIPrincipalScope(
                        business_id=user.business_id, principal_id=principal_id,
                        scope=scope, granted_by_user_id=user.user_id,
                    )
                    session.add(row)
                else:
                    row.granted_by_user_id = user.user_id
                    row.granted_at = now
                    row.revoked_by_user_id = None
                    row.revoked_at = None
            elif row and row.revoked_at is None:
                row.revoked_by_user_id = user.user_id
                row.revoked_at = now
            await record_business_event(
                session, business_id=user.business_id,
                event_type="ai_principal_scope_granted" if grant else "ai_principal_scope_revoked",
                source="crm", actor_type="human", actor_id=user.user_id,
                entity_type="ai_service_principal", entity_id=principal_id,
                data={"scope": scope, "reason": reason},
            )
            await session.commit()
        principals = await self.list_principals(user)
        return next(item for item in principals if item["id"] == principal_id)

    async def list_policies(self, user: AuthenticatedUser) -> list[dict]:
        await self.ensure_defaults(user)
        async with self.session_factory() as session:
            rows = (await session.execute(
                select(AuthorityPolicy, AuthorityPolicyVersion)
                .join(AuthorityPolicyVersion,
                      (AuthorityPolicyVersion.policy_id == AuthorityPolicy.id)
                      & (AuthorityPolicyVersion.version == AuthorityPolicy.active_version))
                .where(AuthorityPolicy.business_id == user.business_id)
                .order_by(AuthorityPolicy.action_type)
            )).all()
        return [self.policy_payload(policy, version) for policy, version in rows]

    async def update_policy(self, user: AuthenticatedUser, policy_id: str, values: dict) -> dict:
        expected_version = values.pop("expected_version")
        reason = values.pop("change_reason")
        self._validate_scope(values["required_scope"])
        await self.ensure_defaults(user)
        async with self.session_factory() as session:
            policy = await session.scalar(select(AuthorityPolicy).where(
                AuthorityPolicy.id == policy_id,
                AuthorityPolicy.business_id == user.business_id,
            ).with_for_update())
            if policy is None:
                raise HTTPException(status_code=404, detail="Authority policy was not found.")
            if policy.active_version != expected_version:
                raise HTTPException(status_code=409, detail=(
                    f"Policy version is {policy.active_version}; expected {expected_version}."
                ))
            new_version = expected_version + 1
            version = AuthorityPolicyVersion(
                business_id=user.business_id,
                policy_id=policy.id,
                action_type=policy.action_type,
                version=new_version,
                decision_mode=values["decision_mode"],
                risk_level=values["risk_level"],
                required_scope=values["required_scope"],
                approval_role=values.get("approval_role"),
                conditions=values.get("conditions", {}),
                change_reason=reason,
                created_by_user_id=user.user_id,
            )
            session.add(version)
            policy.active_version = new_version
            policy.updated_at = utc_now()
            await record_business_event(
                session, business_id=user.business_id,
                event_type="authority_policy_version_published", source="crm",
                actor_type="human", actor_id=user.user_id,
                entity_type="authority_policy", entity_id=policy.id,
                data={"action_type": policy.action_type, "version": new_version,
                      "decision_mode": version.decision_mode, "reason": reason},
            )
            await session.commit()
            return self.policy_payload(policy, version)

    async def policy_history(self, user: AuthenticatedUser, policy_id: str) -> list[dict]:
        async with self.session_factory() as session:
            policy = await session.scalar(select(AuthorityPolicy).where(
                AuthorityPolicy.id == policy_id,
                AuthorityPolicy.business_id == user.business_id,
            ))
            if policy is None:
                raise HTTPException(status_code=404, detail="Authority policy was not found.")
            rows = (await session.scalars(select(AuthorityPolicyVersion).where(
                AuthorityPolicyVersion.policy_id == policy.id,
                AuthorityPolicyVersion.business_id == user.business_id,
            ).order_by(AuthorityPolicyVersion.version.desc()))).all()
        return [{
            "id": row.id, "action_type": row.action_type, "version": row.version,
            "decision_mode": row.decision_mode, "risk_level": row.risk_level,
            "required_scope": row.required_scope, "approval_role": row.approval_role,
            "conditions": row.conditions, "change_reason": row.change_reason,
            "created_by_user_id": row.created_by_user_id,
            "effective_from": iso(row.effective_from), "created_at": iso(row.created_at),
        } for row in rows]

    async def evaluate(
        self, *, business_id: str, principal_id: str, action_type: str, facts: dict
    ) -> dict:
        """Return a deterministic decision. This method never performs the action."""
        async with self.session_factory() as session:
            settings = await session.get(BusinessSettings, business_id)
            principal = await session.scalar(select(AIServicePrincipal).where(
                AIServicePrincipal.id == principal_id,
                AIServicePrincipal.business_id == business_id,
                AIServicePrincipal.status == "active",
                AIServicePrincipal.revoked_at.is_(None),
            ))
            policy_row = (await session.execute(
                select(AuthorityPolicy, AuthorityPolicyVersion)
                .join(AuthorityPolicyVersion,
                      (AuthorityPolicyVersion.policy_id == AuthorityPolicy.id)
                      & (AuthorityPolicyVersion.version == AuthorityPolicy.active_version))
                .where(AuthorityPolicy.business_id == business_id,
                       AuthorityPolicy.action_type == action_type,
                       AuthorityPolicy.enabled.is_(True))
            )).one_or_none()
            if settings is None:
                return {"decision": "deny", "reason": "Business AI settings are not initialized."}
            if principal is None:
                return {"decision": "deny", "reason": "AI principal is inactive or unknown."}
            if policy_row is None:
                return {"decision": "deny", "reason": "No enabled authority policy exists."}
            policy, version = policy_row
            has_scope = await session.scalar(select(AIPrincipalScope.id).where(
                AIPrincipalScope.business_id == business_id,
                AIPrincipalScope.principal_id == principal_id,
                AIPrincipalScope.scope == version.required_scope,
                AIPrincipalScope.revoked_at.is_(None),
            ))
        base = {
            "business_id": business_id, "principal_id": principal_id,
            "action_type": action_type, "policy_id": policy.id,
            "policy_version": version.version, "risk_level": version.risk_level,
            "approval_role": version.approval_role,
        }
        if not has_scope:
            return {**base, "decision": "deny", "reason": f"Missing scope {version.required_scope}."}
        if settings.ai_operating_mode == "recommend_only":
            return {**base, "decision": "recommend_only",
                    "reason": "Business operating mode does not allow execution."}
        if version.decision_mode in {"deny", "recommend_only", "prepare_only", "approval_required"}:
            return {**base, "decision": version.decision_mode,
                    "reason": "Authority policy requires this handling mode."}
        if settings.ai_operating_mode == "prepare_only":
            return {**base, "decision": "prepare_only",
                    "reason": "Business operating mode permits preparation only."}
        if version.decision_mode == "auto_execute":
            return {**base, "decision": "allow", "reason": "Low-risk action is authorized."}
        if version.decision_mode == "threshold_auto":
            margin = decimal_value(facts.get("resulting_margin_pct"), "resulting_margin_pct")
            if margin < settings.minimum_margin_pct:
                return {**base, "decision": "approval_required",
                        "reason": "Resulting margin is below the business minimum."}
            if action_type == "discount_apply":
                discount = decimal_value(facts.get("discount_pct"), "discount_pct")
                allowed = discount <= settings.maximum_automatic_discount_pct
                reason = "Discount is within automatic authority." if allowed else "Discount exceeds automatic authority."
            elif action_type == "quotation_create":
                amount = decimal_value(facts.get("quotation_value"), "quotation_value")
                allowed = amount <= settings.maximum_automatic_quotation_value
                reason = "Quotation is within automatic authority." if allowed else "Quotation exceeds automatic authority."
            else:
                allowed, reason = False, "Threshold policy has no deterministic evaluator."
            return {**base, "decision": "allow" if allowed else "approval_required", "reason": reason}
        return {**base, "decision": "deny", "reason": "Unsupported policy mode."}

    async def evaluate_action(
        self,
        *,
        business_id: str,
        principal_id: str,
        action_type: str,
        facts: dict,
        tool_execution_id: str | None = None,
        action_request_id: str | None = None,
        create_approval: bool = True,
    ) -> AuthorityDecisionResult:
        """Evaluate and persist a standardized deterministic Batch 3 decision."""
        validated = validate_action_facts(action_type, facts)
        facts_payload = validated.model_dump(mode="json")
        approval_facts = dict(facts_payload)
        # Retrieval evidence is audited separately and may vary between
        # equivalent searches; it must not change the commercial facts hash.
        approval_facts.pop("evidence_chunk_ids", None)
        facts_hash = hash_request(approval_facts)
        now = utc_now()

        async with self.session_factory() as session:
            settings = await session.get(BusinessSettings, business_id)
            principal = await session.scalar(select(AIServicePrincipal).where(
                AIServicePrincipal.id == principal_id,
                AIServicePrincipal.business_id == business_id,
                AIServicePrincipal.status == "active",
                AIServicePrincipal.revoked_at.is_(None),
            ))
            policy_row = (await session.execute(
                select(AuthorityPolicy, AuthorityPolicyVersion)
                .join(
                    AuthorityPolicyVersion,
                    (AuthorityPolicyVersion.policy_id == AuthorityPolicy.id)
                    & (AuthorityPolicyVersion.version == AuthorityPolicy.active_version),
                )
                .where(
                    AuthorityPolicy.business_id == business_id,
                    AuthorityPolicy.action_type == action_type,
                    AuthorityPolicy.enabled.is_(True),
                )
            )).one_or_none()

            if settings is None or principal is None or policy_row is None:
                reasons = []
                if settings is None:
                    reasons.append("Business AI settings are not initialized.")
                if principal is None:
                    reasons.append("AI principal is inactive or unknown.")
                if policy_row is None:
                    reasons.append("No enabled authority policy exists for this action.")
                result = AuthorityDecisionResult(
                    decision=AuthorityOutcome.DENY,
                    action_type=action_type,
                    risk_level="critical",
                    policy_code="AUTHORITY_CONFIGURATION_INCOMPLETE",
                    reasons=reasons,
                    evaluated_facts=facts_payload,
                    evidence_chunk_ids=validated.evidence_chunk_ids,
                )
                if principal is None:
                    return result
                return await self._persist_decision(
                    session, business_id=business_id, principal_id=principal_id,
                    result=result, facts_hash=facts_hash,
                    tool_execution_id=tool_execution_id,
                    action_request_id=action_request_id,
                    create_approval=False,
                )

            policy, version = policy_row
            has_scope = await session.scalar(select(AIPrincipalScope.id).where(
                AIPrincipalScope.business_id == business_id,
                AIPrincipalScope.principal_id == principal_id,
                AIPrincipalScope.scope == version.required_scope,
                AIPrincipalScope.revoked_at.is_(None),
            ))
            if not has_scope:
                result = AuthorityDecisionResult(
                    decision=AuthorityOutcome.DENY,
                    action_type=action_type,
                    risk_level=version.risk_level,
                    policy_code="AI_SCOPE_MISSING",
                    policy_id=policy.id,
                    policy_version=version.version,
                    settings_version=settings.version,
                    approval_role=version.approval_role,
                    reasons=[f"Missing scope {version.required_scope}."],
                    evaluated_facts=facts_payload,
                    evidence_chunk_ids=validated.evidence_chunk_ids,
                )
            else:
                approval_query = select(AuthorityApprovalRequest).where(
                        AuthorityApprovalRequest.business_id == business_id,
                        AuthorityApprovalRequest.action_type == action_type,
                        AuthorityApprovalRequest.requested_by_principal_id == principal_id,
                        AuthorityApprovalRequest.facts_hash == facts_hash,
                        AuthorityApprovalRequest.policy_id == policy.id,
                        AuthorityApprovalRequest.policy_version == version.version,
                        AuthorityApprovalRequest.settings_version == settings.version,
                        AuthorityApprovalRequest.status == "APPROVED",
                    )
                if action_request_id is not None:
                    approval_query = approval_query.where(
                        AuthorityApprovalRequest.action_request_id == action_request_id
                    )
                approved = await session.scalar(
                    approval_query.order_by(
                        AuthorityApprovalRequest.approved_at.desc()
                    ).with_for_update()
                )
                if approved is not None and (
                    approved.expires_at is None or approved.expires_at > now
                ):
                    approved.status = "CONSUMED"
                    approved.consumed_at = now
                    result = AuthorityDecisionResult(
                        decision=AuthorityOutcome.ALLOW,
                        action_type=action_type,
                        risk_level=version.risk_level,
                        policy_code="APPROVED_POLICY_EXCEPTION",
                        policy_id=policy.id,
                        policy_version=version.version,
                        settings_version=settings.version,
                        reasons=["A valid human approval for these exact facts was consumed."],
                        evaluated_facts=facts_payload,
                        evidence_chunk_ids=validated.evidence_chunk_ids,
                    )
                else:
                    if approved is not None and approved.expires_at and approved.expires_at <= now:
                        approved.status = "EXPIRED"
                    result = evaluate_policy(
                        action_type=action_type, settings=settings,
                        policy=policy, version=version, facts=validated,
                    )

            return await self._persist_decision(
                session, business_id=business_id, principal_id=principal_id,
                result=result, facts_hash=facts_hash,
                tool_execution_id=tool_execution_id,
                action_request_id=action_request_id,
                create_approval=create_approval,
            )

    async def _persist_decision(
        self, session, *, business_id: str, principal_id: str,
        result: AuthorityDecisionResult, facts_hash: str,
        tool_execution_id: str | None, action_request_id: str | None,
        create_approval: bool,
    ) -> AuthorityDecisionResult:
        facts = result.evaluated_facts
        row = AuthorityDecision(
            business_id=business_id,
            principal_id=principal_id,
            action_request_id=action_request_id,
            action_type=result.action_type,
            entity_type=facts.get("entity_type"),
            entity_id=facts.get("entity_id"),
            tool_execution_id=tool_execution_id,
            thread_id=facts.get("thread_id"),
            decision=result.decision.value,
            risk_level=result.risk_level,
            policy_code=result.policy_code,
            policy_id=result.policy_id,
            policy_version=result.policy_version,
            settings_version=result.settings_version,
            approval_role=result.approval_role,
            facts_snapshot=facts,
            reasons=result.reasons,
            missing_information=result.missing_information,
            missing_master_data=result.missing_master_data,
            evidence_chunk_ids=result.evidence_chunk_ids,
            input_hash=facts_hash,
        )
        session.add(row)
        await session.flush()
        result.decision_id = row.id

        if result.decision == AuthorityOutcome.REQUIRE_APPROVAL and create_approval:
            approval = AuthorityApprovalRequest(
                business_id=business_id,
                authority_decision_id=row.id,
                action_request_id=action_request_id,
                action_type=result.action_type,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                thread_id=row.thread_id,
                requested_by_principal_id=principal_id,
                required_role=result.approval_role or "admin",
                status="PENDING",
                reason=result.reason or "Human approval is required.",
                policy_id=result.policy_id,
                policy_version=result.policy_version,
                settings_version=result.settings_version,
                facts_hash=facts_hash,
                expires_at=utc_now() + timedelta(hours=24),
            )
            session.add(approval)
            await session.flush()
            result.approval_request_id = approval.id

        await record_business_event(
            session,
            business_id=business_id,
            thread_id=row.thread_id,
            event_type="authority.decision_recorded",
            source="authority_engine",
            actor_type="ai_principal",
            actor_id=principal_id,
            entity_type=row.entity_type or "authority_decision",
            entity_id=row.entity_id or row.id,
            data={
                "decision_id": row.id,
                "decision": result.decision.value,
                "action_type": result.action_type,
                "policy_code": result.policy_code,
                "policy_version": result.policy_version,
                "approval_request_id": result.approval_request_id,
            },
        )
        await session.commit()
        return result

    async def list_decisions(self, user: AuthenticatedUser, *, limit: int = 100) -> list[dict]:
        async with self.session_factory() as session:
            rows = (await session.scalars(
                select(AuthorityDecision).where(
                    AuthorityDecision.business_id == user.business_id
                ).order_by(AuthorityDecision.created_at.desc()).limit(limit)
            )).all()
        return [self.decision_payload(row) for row in rows]

    async def get_decision(self, user: AuthenticatedUser, decision_id: str) -> dict:
        async with self.session_factory() as session:
            row = await session.scalar(select(AuthorityDecision).where(
                AuthorityDecision.id == decision_id,
                AuthorityDecision.business_id == user.business_id,
            ))
        if row is None:
            raise HTTPException(status_code=404, detail="Authority decision was not found.")
        return self.decision_payload(row)

    @staticmethod
    def decision_payload(row: AuthorityDecision) -> dict:
        return {
            "id": row.id, "business_id": row.business_id,
            "action_request_id": row.action_request_id,
            "principal_id": row.principal_id, "action_type": row.action_type,
            "decision": row.decision, "risk_level": row.risk_level,
            "policy_code": row.policy_code, "policy_id": row.policy_id,
            "policy_version": row.policy_version,
            "settings_version": row.settings_version,
            "approval_role": row.approval_role,
            "entity_type": row.entity_type, "entity_id": row.entity_id,
            "thread_id": row.thread_id, "facts": row.facts_snapshot,
            "reasons": row.reasons,
            "missing_information": row.missing_information,
            "missing_master_data": row.missing_master_data,
            "evidence_chunk_ids": row.evidence_chunk_ids,
            "created_at": iso(row.created_at),
        }

    async def list_approval_requests(
        self, user: AuthenticatedUser, *, status: str | None = "PENDING", limit: int = 100
    ) -> list[dict]:
        async with self.session_factory() as session:
            query = select(AuthorityApprovalRequest).where(
                AuthorityApprovalRequest.business_id == user.business_id
            )
            if status:
                query = query.where(AuthorityApprovalRequest.status == status.upper())
            rows = (await session.scalars(
                query.order_by(AuthorityApprovalRequest.created_at.desc()).limit(limit)
            )).all()
        return [self.approval_payload(row) for row in rows]

    @staticmethod
    def approval_payload(row: AuthorityApprovalRequest) -> dict:
        return {
            "id": row.id, "business_id": row.business_id,
            "authority_decision_id": row.authority_decision_id,
            "action_request_id": row.action_request_id,
            "action_type": row.action_type, "entity_type": row.entity_type,
            "entity_id": row.entity_id, "thread_id": row.thread_id,
            "required_role": row.required_role, "status": row.status,
            "reason": row.reason, "policy_id": row.policy_id,
            "policy_version": row.policy_version,
            "settings_version": row.settings_version,
            "expires_at": iso(row.expires_at), "approved_at": iso(row.approved_at),
            "rejected_at": iso(row.rejected_at),
            "resolution_reason": row.resolution_reason,
            "created_at": iso(row.created_at),
        }

    async def resolve_approval(
        self, user: AuthenticatedUser, approval_id: str, *, approve: bool, reason: str
    ) -> dict:
        async with self.session_factory() as session:
            row = await session.scalar(select(AuthorityApprovalRequest).where(
                AuthorityApprovalRequest.id == approval_id,
                AuthorityApprovalRequest.business_id == user.business_id,
            ).with_for_update())
            if row is None:
                raise HTTPException(status_code=404, detail="Approval request was not found.")
            if row.status != "PENDING":
                raise HTTPException(status_code=409, detail=f"Approval is already {row.status}.")
            role_allowed = user.role == "admin" or user.role == row.required_role
            if not role_allowed:
                raise HTTPException(
                    status_code=403,
                    detail=f"Role '{user.role}' cannot resolve an approval for '{row.required_role}'.",
                )
            now = utc_now()
            if row.expires_at and row.expires_at <= now:
                row.status = "EXPIRED"
                if row.action_request_id:
                    action = await session.get(AIActionRequest, row.action_request_id)
                    if action and action.status == AIActionStatus.AWAITING_APPROVAL:
                        assert_transition(action.status, AIActionStatus.EXPIRED)
                        action.status = AIActionStatus.EXPIRED
                        action.completed_at = now
                await session.commit()
                raise HTTPException(status_code=409, detail="Approval request has expired.")
            row.status = "APPROVED" if approve else "REJECTED"
            row.resolution_reason = reason
            if approve:
                row.approved_by_user_id = user.user_id
                row.approved_at = now
            else:
                row.rejected_by_user_id = user.user_id
                row.rejected_at = now
            session.add(ApprovalDecision(
                business_id=user.business_id,
                action_request_id=row.action_request_id,
                approval_request_id=row.id,
                authority_decision_id=row.authority_decision_id,
                decision=row.status,
                decided_by_user_id=user.user_id,
                decided_by_role=user.role,
                reason=reason,
                policy_id=row.policy_id,
                policy_version=row.policy_version,
                settings_version=row.settings_version,
                facts_hash=row.facts_hash,
                decided_at=now,
            ))
            if row.action_request_id:
                action = await session.scalar(select(AIActionRequest).where(
                    AIActionRequest.id == row.action_request_id,
                    AIActionRequest.business_id == user.business_id,
                ).with_for_update())
                if action is None:
                    raise HTTPException(status_code=409, detail="Approval action ledger is missing.")
                target = AIActionStatus.APPROVED if approve else AIActionStatus.REJECTED
                assert_transition(action.status, target)
                action.status = target
                action.approved_at = now if approve else None
                action.completed_at = None if approve else now
            await record_business_event(
                session, business_id=user.business_id,
                event_type="authority.approval_resolved", source="crm",
                actor_type="human", actor_id=user.user_id,
                entity_type="authority_approval", entity_id=row.id,
                thread_id=row.thread_id,
                data={"status": row.status, "action_type": row.action_type,
                      "reason": reason, "policy_version": row.policy_version},
            )
            await session.commit()
            return self.approval_payload(row)
