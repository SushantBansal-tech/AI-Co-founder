from decimal import Decimal

from app.authority.decisions import AuthorityDecisionResult, AuthorityOutcome
from app.authority.facts import (
    ActionFacts,
    CommercialFacts,
    CreditChangeFacts,
    CustomerMergeFacts,
    DealCloseFacts,
    MessageFacts,
    PurchaseOrderFacts,
    SalesOrderFacts,
)


def _result(
    outcome: AuthorityOutcome,
    *,
    action_type: str,
    policy_code: str,
    settings,
    policy,
    version,
    facts: ActionFacts,
    reasons: list[str],
    approval_role: str | None = None,
) -> AuthorityDecisionResult:
    return AuthorityDecisionResult(
        decision=outcome,
        action_type=action_type,
        risk_level=version.risk_level,
        policy_code=policy_code,
        policy_id=policy.id,
        policy_version=version.version,
        settings_version=settings.version,
        approval_role=approval_role,
        reasons=reasons,
        missing_information=facts.missing_information,
        missing_master_data=facts.missing_master_data,
        evaluated_facts=facts.model_dump(mode="json"),
        evidence_chunk_ids=facts.evidence_chunk_ids,
    )


def evaluate_policy(*, action_type: str, settings, policy, version, facts: ActionFacts):
    args = {
        "action_type": action_type,
        "settings": settings,
        "policy": policy,
        "version": version,
        "facts": facts,
    }
    if facts.missing_master_data:
        return _result(
            AuthorityOutcome.BLOCKED_MASTER_DATA,
            policy_code="REQUIRED_MASTER_DATA_MISSING",
            reasons=["Required authoritative master data is missing."],
            **args,
        )
    if facts.missing_information:
        return _result(
            AuthorityOutcome.REQUIRE_MORE_INFORMATION,
            policy_code="ACTION_INFORMATION_INCOMPLETE",
            reasons=["Required action information is incomplete."],
            **args,
        )
    if settings.ai_operating_mode == "recommend_only":
        return _result(
            AuthorityOutcome.DENY,
            policy_code="OPERATING_MODE_RECOMMEND_ONLY",
            reasons=["Business operating mode permits recommendations only."],
            **args,
        )
    if version.decision_mode in {"deny", "recommend_only"}:
        return _result(
            AuthorityOutcome.DENY,
            policy_code="POLICY_DENIES_EXECUTION",
            reasons=["The active authority policy does not permit execution."],
            **args,
        )
    if settings.ai_operating_mode == "prepare_only" and action_type not in {
        "prepare_quotation", "quotation_prepare", "negotiation_response"
    }:
        return _result(
            AuthorityOutcome.DENY,
            policy_code="OPERATING_MODE_PREPARE_ONLY",
            reasons=["Business operating mode permits preparation but not execution."],
            **args,
        )

    if isinstance(facts, MessageFacts):
        if facts.customer_opted_out:
            return _result(AuthorityOutcome.DENY, policy_code="CUSTOMER_OPTED_OUT",
                           reasons=["The customer has opted out of this communication."], **args)
        if not facts.recipient:
            facts.missing_information.append("recipient")
            return _result(AuthorityOutcome.REQUIRE_MORE_INFORMATION,
                           policy_code="MESSAGE_RECIPIENT_MISSING",
                           reasons=["A validated recipient is required."], **args)
        if not facts.provider_configured:
            facts.missing_master_data.append("channel_provider_configuration")
            return _result(AuthorityOutcome.BLOCKED_MASTER_DATA,
                           policy_code="CHANNEL_PROVIDER_NOT_CONFIGURED",
                           reasons=["The selected channel provider is not configured."], **args)
        if facts.outbound_count_today >= settings.daily_outbound_message_limit:
            return _result(AuthorityOutcome.DENY, policy_code="DAILY_MESSAGE_LIMIT_REACHED",
                           reasons=["The business daily outbound-message limit was reached."], **args)
        if facts.contains_commercial_commitment:
            return _result(AuthorityOutcome.REQUIRE_APPROVAL,
                           policy_code="COMMERCIAL_COMMITMENT_REQUIRES_APPROVAL",
                           reasons=["The message contains a commercial commitment."],
                           approval_role=version.approval_role or settings.default_approval_role,
                           **args)

    if isinstance(facts, CommercialFacts):
        margin = facts.resulting_margin_pct
        if facts.proposed_price_per_unit is not None and facts.floor_price_per_unit is not None:
            if facts.proposed_price_per_unit < facts.floor_price_per_unit:
                return _result(AuthorityOutcome.DENY,
                               policy_code="PRICE_BELOW_ABSOLUTE_FLOOR",
                               reasons=["The proposed price is below the absolute floor price."], **args)
        if margin is not None and margin < settings.minimum_margin_pct:
            return _result(AuthorityOutcome.REQUIRE_APPROVAL,
                           policy_code="MARGIN_BELOW_AUTOMATIC_AUTHORITY",
                           reasons=[
                               f"Resulting margin is {margin}%.",
                               f"Business minimum margin is {settings.minimum_margin_pct}%.",
                           ], approval_role=version.approval_role or settings.default_approval_role,
                           **args)
        if action_type == "discount_apply" and facts.discount_pct > settings.maximum_automatic_discount_pct:
            return _result(AuthorityOutcome.REQUIRE_APPROVAL,
                           policy_code="DISCOUNT_ABOVE_AI_AUTHORITY",
                           reasons=[
                               f"Requested discount is {facts.discount_pct}%.",
                               f"Automatic authority is {settings.maximum_automatic_discount_pct}%.",
                           ], approval_role=version.approval_role or settings.default_approval_role,
                           **args)
        if action_type in {"quotation_create", "quotation_send"} and (
            facts.quotation_value > settings.maximum_automatic_quotation_value
        ):
            return _result(AuthorityOutcome.REQUIRE_APPROVAL,
                           policy_code="QUOTATION_ABOVE_AI_AUTHORITY",
                           reasons=[
                               f"Quotation value is {facts.quotation_value}.",
                               f"Automatic authority is {settings.maximum_automatic_quotation_value}.",
                           ], approval_role=version.approval_role or settings.default_approval_role,
                           **args)

    if isinstance(facts, PurchaseOrderFacts):
        if not facts.mandatory_fields_complete:
            return _result(AuthorityOutcome.REQUIRE_MORE_INFORMATION,
                           policy_code="PO_REQUIRED_FIELDS_MISSING",
                           reasons=["The purchase order is missing required fields."], **args)
        if facts.critical_mismatches:
            return _result(AuthorityOutcome.DENY, policy_code="PO_CRITICAL_MISMATCH",
                           reasons=facts.critical_mismatches, **args)
        revalidation = {
            "quotation": facts.quotation_is_current,
            "price": facts.price_revalidated,
            "inventory": facts.inventory_revalidated,
            "capacity": facts.capacity_revalidated,
            "delivery": facts.delivery_revalidated,
            "credit": facts.credit_revalidated,
            "approvals": facts.approvals_valid,
        }
        failed = [name for name, passed in revalidation.items() if not passed]
        if failed:
            return _result(AuthorityOutcome.REQUIRE_APPROVAL,
                           policy_code="PO_REVALIDATION_CHANGED",
                           reasons=["Revalidation failed for: " + ", ".join(failed)],
                           approval_role=version.approval_role or settings.default_approval_role,
                           **args)
        if facts.minor_mismatches:
            return _result(AuthorityOutcome.REQUIRE_APPROVAL,
                           policy_code="PO_MINOR_MISMATCH_REVIEW",
                           reasons=facts.minor_mismatches,
                           approval_role=version.approval_role or settings.default_approval_role,
                           **args)

    if isinstance(facts, SalesOrderFacts):
        if not facts.po_validation_passed:
            return _result(AuthorityOutcome.DENY, policy_code="PO_NOT_VALIDATED",
                           reasons=["A validated purchase order is required."], **args)
        if not (facts.inventory_reserved or facts.production_allocated):
            return _result(AuthorityOutcome.BLOCKED_MASTER_DATA,
                           policy_code="FULFILLMENT_NOT_ALLOCATED",
                           reasons=["Inventory reservation or production allocation is required."], **args)

    if isinstance(facts, CustomerMergeFacts):
        if not facts.source_customer_id or not facts.target_customer_id:
            return _result(AuthorityOutcome.REQUIRE_MORE_INFORMATION,
                           policy_code="MERGE_CUSTOMER_IDS_MISSING",
                           reasons=["Source and target customer IDs are required."], **args)
        if facts.conflicting_identities:
            return _result(AuthorityOutcome.DENY,
                           policy_code="CUSTOMER_IDENTITIES_CONFLICT",
                           reasons=facts.conflicting_identities, **args)
        return _result(AuthorityOutcome.REQUIRE_APPROVAL,
                       policy_code="CUSTOMER_MERGE_REQUIRES_APPROVAL",
                       reasons=["Permanent customer merges require human approval."],
                       approval_role=version.approval_role or settings.default_approval_role,
                       **args)

    if isinstance(facts, CreditChangeFacts):
        if facts.current_exposure is None or facts.overdue_amount is None:
            facts.missing_master_data.extend([
                name for name, value in {
                    "current_exposure": facts.current_exposure,
                    "overdue_amount": facts.overdue_amount,
                }.items() if value is None
            ])
            return _result(AuthorityOutcome.BLOCKED_MASTER_DATA,
                           policy_code="CREDIT_MASTER_DATA_INCOMPLETE",
                           reasons=["Credit exposure and overdue data are required."], **args)
        return _result(AuthorityOutcome.REQUIRE_APPROVAL,
                       policy_code="CREDIT_CHANGE_REQUIRES_APPROVAL",
                       reasons=["Changes to customer credit require human approval."],
                       approval_role=version.approval_role or settings.default_approval_role,
                       **args)

    if isinstance(facts, DealCloseFacts):
        if action_type == "deal_close_won" and not (
            facts.valid_contractual_acceptance
            and facts.po_validated
            and facts.revalidation_passed
        ):
            return _result(AuthorityOutcome.DENY,
                           policy_code="ORDER_ACCEPTANCE_NOT_COMPLETE",
                           reasons=[
                               "A deal cannot be closed as won until contractual "
                               "acceptance, PO validation and revalidation are complete."
                           ], **args)
        if action_type == "deal_close_lost" and not facts.reason_code:
            facts.missing_information.append("reason_code")
            return _result(AuthorityOutcome.REQUIRE_MORE_INFORMATION,
                           policy_code="LOST_REASON_REQUIRED",
                           reasons=["A controlled lost-deal reason is required."], **args)

    if version.decision_mode == "approval_required":
        return _result(
            AuthorityOutcome.REQUIRE_APPROVAL,
            policy_code="POLICY_REQUIRES_APPROVAL",
            reasons=["The active authority policy requires human approval."],
            approval_role=version.approval_role,
            **args,
        )

    # Preparation-only policies authorize preparation tools but never imply
    # permission to dispatch their output.
    code = "PREPARATION_AUTHORIZED" if version.decision_mode == "prepare_only" else "ACTION_WITHIN_AUTHORITY"
    return _result(AuthorityOutcome.ALLOW, policy_code=code,
                   reasons=["The action is within the active authority policy."], **args)
