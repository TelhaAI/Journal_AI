from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import DB, USER
from ..lookback.service import RecordChangedError, replay_receipt
from ..models import LookbackReceipt, LookbackReport
from ..schemas import LookbackIn

router = APIRouter(tags=["lookback"])


def _report_out(report: LookbackReport, receipt: LookbackReceipt, evidence: dict | None = None) -> dict:
    ev = evidence if evidence is not None else receipt.evidence_table
    return {
        "report_id": report.id,
        "outcome": receipt.outcome,
        "observations": report.observations,
        "message": report.message,
        "based_on": {"entry_count": ev.get("entry_count", 0), "range": ev.get("range"),
                     "entry_ids": receipt.input_entry_ids},
        "evidence_table": ev,
        "prompt_version_id": report.prompt_version_id,
        "model": report.model,
        "gate_version": receipt.gate_version,
        "regenerated": receipt.regenerated,
        "created_at": report.created_at.isoformat(),
    }


@router.post("/lookback")
async def run_lookback(payload: LookbackIn, request: Request, db: Session = DB, user_id: str = USER):
    svc = request.app.state.lookback
    start = payload.from_.replace(tzinfo=None) if payload.from_ else None
    end = payload.to.replace(tzinfo=None) if payload.to else None
    out = await svc.run(db, user_id, volume_ids=payload.volume_ids, start=start, end=end)
    return _report_out(out.report, out.receipt, out.evidence)


@router.get("/lookback")
def list_reports(db: Session = DB, user_id: str = USER):
    rows = db.scalars(select(LookbackReport).where(LookbackReport.user_id == user_id)
                      .order_by(LookbackReport.created_at.desc())).all()
    return [{"report_id": r.id, "created_at": r.created_at.isoformat(), "observations": len(r.observations),
             "outcome": r.receipt.outcome if r.receipt else None} for r in rows]


@router.get("/lookback/{report_id}")
def get_report(report_id: str, db: Session = DB, user_id: str = USER):
    r = db.get(LookbackReport, report_id)
    if r is None or r.user_id != user_id:
        raise HTTPException(status_code=404, detail="not found")
    return _report_out(r, r.receipt)


@router.get("/lookback/{report_id}/receipt")
def get_receipt(report_id: str, db: Session = DB, user_id: str = USER):
    r = db.get(LookbackReport, report_id)
    if r is None or r.user_id != user_id or r.receipt is None:
        raise HTTPException(status_code=404, detail="not found")
    rc = r.receipt
    return {"receipt_id": rc.id, "report_id": r.id, "input_entry_ids": rc.input_entry_ids,
            "input_content_hash": rc.input_content_hash, "evidence_table": rc.evidence_table,
            "raw_model_output": rc.raw_model_output, "verification_log": rc.verification_log, "dropped": rc.dropped,
            "regenerated": rc.regenerated, "outcome": rc.outcome, "prompt_version_id": rc.prompt_version_id,
            "model": rc.model, "gate_version": rc.gate_version, "created_at": rc.created_at.isoformat()}


@router.post("/lookback/{report_id}/replay")
def replay(report_id: str, db: Session = DB, user_id: str = USER):
    r = db.get(LookbackReport, report_id)
    if r is None or r.user_id != user_id or r.receipt is None:
        raise HTTPException(status_code=404, detail="not found")
    try:
        ok = replay_receipt(db, r.receipt)
    except RecordChangedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"report_id": r.id, "reproduced": ok, "gate_version": r.receipt.gate_version}
