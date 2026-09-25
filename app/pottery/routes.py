from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.deps import current_user
from app.pottery.schemas import (
    ConfirmIn,
    ContextCreate,
    ExclusionCreate,
    ImportIn,
    MergeIn,
    PlanCreate,
    RejectIn,
    RuleSetCreate,
    SherdCreate,
    SplitIn,
)
from app.pottery.service import PotteryService

router = APIRouter(prefix="/api/projects/{project_id}/pottery", tags=["pottery"])

Cursor = str | None


@router.post("/contexts", status_code=201)
def create_context(project_id: int, payload: ContextCreate, user=Depends(current_user)):
    return PotteryService().create_context(project_id, user["id"], payload.model_dump())


@router.get("/contexts")
def list_contexts(project_id: int, cursor: Cursor = None, limit: int = Query(20, ge=1, le=100), sort: str = "code", user=Depends(current_user)):
    return PotteryService().list_contexts(project_id, user["id"], cursor=cursor, limit=limit, sort=sort)


@router.post("/context-exclusions", status_code=201)
def create_exclusion(project_id: int, payload: ExclusionCreate, user=Depends(current_user)):
    return PotteryService().create_exclusion(project_id, user["id"], payload.model_dump())


@router.get("/context-exclusions")
def list_exclusions(project_id: int, user=Depends(current_user)):
    return PotteryService().list_exclusions(project_id, user["id"])


@router.post("/sherds", status_code=201)
def create_sherd(project_id: int, payload: SherdCreate, user=Depends(current_user)):
    return PotteryService().create_sherd(project_id, user["id"], payload.model_dump())


@router.get("/sherds")
def list_sherds(project_id: int, cursor: Cursor = None, limit: int = Query(20, ge=1, le=100), sort: str = "code", user=Depends(current_user)):
    return PotteryService().list_sherds(project_id, user["id"], cursor=cursor, limit=limit, sort=sort)


@router.get("/sherds/{sherd_id}")
def get_sherd(project_id: int, sherd_id: int, user=Depends(current_user)):
    return PotteryService().get_sherd(project_id, user["id"], sherd_id)


@router.post("/rule-sets", status_code=201)
def create_rule_set(project_id: int, payload: RuleSetCreate, user=Depends(current_user)):
    return PotteryService().create_rule_set(project_id, user["id"], payload.model_dump())


@router.get("/rule-sets")
def list_rule_sets(project_id: int, cursor: Cursor = None, limit: int = Query(20, ge=1, le=100), sort: str = "version", user=Depends(current_user)):
    return PotteryService().list_rule_sets(project_id, user["id"], cursor=cursor, limit=limit, sort=sort)


@router.post("/candidates/generate")
def generate_candidates(project_id: int, user=Depends(current_user)):
    return PotteryService().generate_candidates(project_id, user["id"])


@router.get("/candidates")
def list_candidates(
    project_id: int,
    cursor: Cursor = None,
    limit: int = Query(20, ge=1, le=100),
    sort: str = "id",
    status: str | None = Query(default=None, pattern="^(pending|confirmed|rejected)$"),
    user=Depends(current_user),
):
    return PotteryService().list_candidates(project_id, user["id"], cursor=cursor, limit=limit, sort=sort, status=status)


@router.get("/candidates/{candidate_id}")
def get_candidate(project_id: int, candidate_id: int, user=Depends(current_user)):
    return PotteryService().get_candidate(project_id, user["id"], candidate_id)


@router.post("/candidates/{candidate_id}/confirm")
def confirm_candidate(project_id: int, candidate_id: int, payload: ConfirmIn, user=Depends(current_user)):
    return PotteryService().confirm_candidate(project_id, candidate_id, user["id"], payload.rationale, payload.expected_rule_set_id)


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(project_id: int, candidate_id: int, payload: RejectIn, user=Depends(current_user)):
    return PotteryService().reject_candidate(project_id, candidate_id, user["id"], payload.rationale)


@router.get("/vessels")
def list_vessels(
    project_id: int,
    cursor: Cursor = None,
    limit: int = Query(20, ge=1, le=100),
    sort: str = "code",
    status: str | None = Query(default=None, pattern="^(active|dissolved)$"),
    user=Depends(current_user),
):
    return PotteryService().list_vessels(project_id, user["id"], cursor=cursor, limit=limit, sort=sort, status=status)


@router.get("/vessels/{vessel_id}")
def get_vessel(project_id: int, vessel_id: int, user=Depends(current_user)):
    return PotteryService().get_vessel(project_id, user["id"], vessel_id)


@router.post("/vessels/merge")
def merge_vessels(project_id: int, payload: MergeIn, user=Depends(current_user)):
    return PotteryService().merge_vessels(project_id, user["id"], payload.vessel_ids, payload.rationale)


@router.post("/vessels/{vessel_id}/split")
def split_vessel(project_id: int, vessel_id: int, payload: SplitIn, user=Depends(current_user)):
    return PotteryService().split_vessel(project_id, vessel_id, user["id"], payload.groups, payload.rationale)


@router.get("/reviews")
def list_reviews(project_id: int, cursor: Cursor = None, limit: int = Query(20, ge=1, le=100), sort: str = "id", user=Depends(current_user)):
    return PotteryService().list_reviews(project_id, user["id"], cursor=cursor, limit=limit, sort=sort)


@router.post("/plans", status_code=201)
def create_plan(project_id: int, payload: PlanCreate, user=Depends(current_user)):
    return PotteryService().create_plan(project_id, user["id"], payload.model_dump())


@router.get("/plans")
def list_plans(project_id: int, cursor: Cursor = None, limit: int = Query(20, ge=1, le=100), sort: str = "code", user=Depends(current_user)):
    return PotteryService().list_plans(project_id, user["id"], cursor=cursor, limit=limit, sort=sort)


@router.post("/plans/{plan_id}/reports")
def compute_report(project_id: int, plan_id: int, response: Response, user=Depends(current_user)):
    result = PotteryService().compute_report(project_id, plan_id, user["id"])
    response.status_code = 201 if result["created_new"] else 200
    return result


@router.get("/plans/{plan_id}/reports")
def list_reports(project_id: int, plan_id: int, cursor: Cursor = None, limit: int = Query(20, ge=1, le=100), sort: str = "version", user=Depends(current_user)):
    return PotteryService().list_reports(project_id, plan_id, user["id"], cursor=cursor, limit=limit, sort=sort)


@router.get("/plans/{plan_id}/reports/{version}")
def get_report(project_id: int, plan_id: int, version: int, user=Depends(current_user)):
    return PotteryService().get_report(project_id, plan_id, version, user["id"])


@router.get("/plans/{plan_id}/reports/{version}/diff/{other_version}")
def diff_reports(project_id: int, plan_id: int, version: int, other_version: int, user=Depends(current_user)):
    return PotteryService().diff_reports(project_id, plan_id, version, other_version, user["id"])


@router.post("/imports")
def import_batch(project_id: int, payload: ImportIn, response: Response, user=Depends(current_user)):
    result = PotteryService().import_batch(project_id, user["id"], payload.model_dump())
    response.status_code = 200 if result["idempotent"] else 201
    return result


@router.get("/imports")
def list_imports(project_id: int, cursor: Cursor = None, limit: int = Query(20, ge=1, le=100), sort: str = "id", user=Depends(current_user)):
    return PotteryService().list_imports(project_id, user["id"], cursor=cursor, limit=limit, sort=sort)
