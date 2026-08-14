from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.authority.auth import AuthenticatedAIPrincipal, require_ai_principal
from app.business_tools.executor import BusinessToolExecutor
from app.business_tools.schemas import ExecuteToolRequest


router = APIRouter(prefix="/ai/tools", tags=["Controlled Jarvis business tools"])


def _executor(request: Request) -> BusinessToolExecutor:
    executor = getattr(request.app.state, "business_tool_executor", None)
    if executor is None:
        raise HTTPException(status_code=503, detail="Business tool executor is unavailable.")
    return executor


@router.get("")
async def list_controlled_tools(
    request: Request,
    principal: AuthenticatedAIPrincipal = Depends(require_ai_principal),
):
    return {"items": await _executor(request).catalog(principal)}


@router.post("/{tool_name}/execute")
async def execute_controlled_tool(
    tool_name: str,
    body: ExecuteToolRequest,
    request: Request,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=8, max_length=200
    ),
    principal: AuthenticatedAIPrincipal = Depends(require_ai_principal),
):
    return await _executor(request).execute(
        principal=principal, tool_name=tool_name,
        raw_arguments=body.arguments,
        idempotency_key=idempotency_key,
    )
