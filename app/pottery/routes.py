from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.deps import current_user
from app.pottery.schemas import ConfirmAction, ImportBundle, MergeAction, PlanRun, ReviewAction, RuleSetCreate, SherdCreate, SherdUpdate
from app.pottery.service import PotteryService

router = APIRouter(prefix="/api/projects/{project_id}/pottery", tags=["pottery"])

Limit = Query(default=50, ge=1, le=200)


@router.post("/rule-sets", status_code=201)
def create_rule_set(project_id: int, payload: RuleSetCreate, user=Depends(current_user)):
    return PotteryService().create_rule_set(project_id, user["id"], payload.name, payload.config)


@router.get("/rule-sets")
def list_rule_sets(project_id: int, cursor: str = "", limit: int = Limit, user=Depends(current_user)):
    return PotteryService().list_rule_sets(project_id, user["id"], cursor, limit)


@router.post("/sherds", status_code=201)
def create_sherd(project_id: int, payload: SherdCreate, user=Depends(current_user)):
    return PotteryService().create_sherd(project_id, user["id"], payload.model_dump())


@router.get("/sherds")
def list_sherds(
    project_id: int,
    cursor: str = "",
    limit: int = Limit,
    part: str | None = None,
    context_code: str | None = None,
    user=Depends(current_user),
):
    return PotteryService().list_sherds(project_id, user["id"], cursor, limit, part, context_code)


@router.get("/sherds/{sherd_id}")
def get_sherd(project_id: int, sherd_id: int, user=Depends(current_user)):
    return PotteryService().get_sherd(project_id, user["id"], sherd_id)


@router.put("/sherds/{sherd_id}")
def update_sherd(project_id: int, sherd_id: int, payload: SherdUpdate, user=Depends(current_user)):
    return PotteryService().update_sherd(project_id, user["id"], sherd_id, payload.model_dump(exclude_unset=True))


@router.post("/candidates/generate")
def generate_candidates(project_id: int, user=Depends(current_user)):
    return PotteryService().generate_candidates(project_id, user["id"])


@router.get("/candidates")
def list_candidates(project_id: int, cursor: str = "", limit: int = Limit, status: str | None = None, user=Depends(current_user)):
    return PotteryService().list_candidates(project_id, user["id"], cursor, limit, status)


@router.get("/candidates/{group_id}")
def get_candidate(project_id: int, group_id: int, user=Depends(current_user)):
    return PotteryService().get_candidate(project_id, user["id"], group_id)


@router.post("/candidates/{group_id}/confirm")
def confirm_candidate(project_id: int, group_id: int, payload: ConfirmAction, user=Depends(current_user)):
    return PotteryService().confirm_candidate(project_id, user["id"], group_id, payload.rationale, payload.vessel_code)


@router.post("/candidates/{group_id}/reject")
def reject_candidate(project_id: int, group_id: int, payload: ReviewAction, user=Depends(current_user)):
    return PotteryService().reject_candidate(project_id, user["id"], group_id, payload.rationale)


@router.post("/vessels/merge")
def merge_vessels(project_id: int, payload: MergeAction, user=Depends(current_user)):
    return PotteryService().merge_vessels(project_id, user["id"], payload.vessel_ids, payload.rationale, payload.vessel_code)


@router.get("/vessels")
def list_vessels(project_id: int, cursor: str = "", limit: int = Limit, status: str | None = None, user=Depends(current_user)):
    return PotteryService().list_vessels(project_id, user["id"], cursor, limit, status)


@router.get("/vessels/{vessel_id}")
def get_vessel(project_id: int, vessel_id: int, user=Depends(current_user)):
    return PotteryService().get_vessel(project_id, user["id"], vessel_id)


@router.post("/vessels/{vessel_id}/split")
def split_vessel(project_id: int, vessel_id: int, payload: ReviewAction, user=Depends(current_user)):
    return PotteryService().split_vessel(project_id, user["id"], vessel_id, payload.rationale)


@router.post("/stats/run")
def run_stats(project_id: int, payload: PlanRun, user=Depends(current_user)):
    return PotteryService().run_stats(project_id, user["id"], payload.model_dump())


@router.get("/stats/runs")
def list_stat_runs(project_id: int, cursor: str = "", limit: int = Limit, plan_code: str | None = None, user=Depends(current_user)):
    return PotteryService().list_stat_runs(project_id, user["id"], cursor, limit, plan_code)


@router.get("/stats/runs/{run_id}")
def get_stat_run(project_id: int, run_id: int, user=Depends(current_user)):
    return PotteryService().get_stat_run(project_id, user["id"], run_id)


@router.get("/stats/diff")
def diff_stats(project_id: int, plan_code: str, from_version: int, to_version: int, user=Depends(current_user)):
    return PotteryService().diff_stats(project_id, user["id"], plan_code, from_version, to_version)


@router.post("/import")
def import_bundle(project_id: int, payload: ImportBundle, user=Depends(current_user)):
    return PotteryService().import_bundle(project_id, user["id"], payload.model_dump())
