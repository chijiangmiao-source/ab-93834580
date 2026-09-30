"""审计服务 HTTP API 与页面入口。"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core import run_audit, validate_audit_id
from .storage import FrozenStore, contract_hash

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="星载递归载荷兼容审计", version="1.0.0")
_store = FrozenStore()


class AuditSubmission(BaseModel):
    audit_id: str = Field(..., description="稳定审计标识")
    # 形状校验交给核心引擎，使“不是数组”等问题与其它静态问题一样一次性反馈。
    sender: Any
    receiver: Any


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "frozen_audits": _store.count()}


@app.post("/api/audits")
def submit_audit(sub: AuditSubmission) -> JSONResponse:
    audit_id = validate_audit_id(sub.audit_id)
    if audit_id is None:
        return JSONResponse(
            status_code=400,
            content={
                "conclusion": "invalid",
                "errors": [
                    {
                        "code": "bad_audit_identifier",
                        "where": "audit_id",
                        "message": "非法审计标识：须以字母数字开头，仅含字母数字 . _ -，长度 1–128",
                    }
                ],
            },
        )

    report = run_audit({"sender": sub.sender, "receiver": sub.receiver})

    # 已冻结标识优先裁决：契约一旦变化（哪怕新契约非法）一律 409，绝不改写。
    existing = _store.get(audit_id)
    if existing is not None:
        if existing["contract_hash"] != contract_hash(sub.sender, sub.receiver):
            return JSONResponse(
                status_code=409,
                content={
                    "error": "contract_changed",
                    "message": (
                        f"审计标识 {audit_id} 已冻结于 {existing['created_at']}；"
                        "契约发生变化，拒绝且不改写原结论"
                    ),
                    "audit_id": audit_id,
                    "frozen_created_at": existing["created_at"],
                    "frozen_report": existing["report"],
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "audit_id": audit_id,
                "created": False,
                "reused_frozen": True,
                "created_at": existing["created_at"],
                "contract_hash": existing["contract_hash"],
                **existing["report"],
            },
        )

    # 新标识 + 非法契约：反馈问题但不占用/冻结标识，允许修正后重提。
    if report["conclusion"] == "invalid":
        return JSONResponse(status_code=200, content={"audit_id": audit_id, **report})

    record, created, _conflict = _store.submit(audit_id, sub.sender, sub.receiver, report)

    status = 201 if created else 200
    return JSONResponse(
        status_code=status,
        content={
            "audit_id": audit_id,
            "created": created,
            "reused_frozen": not created,
            "created_at": record["created_at"],
            "contract_hash": record["contract_hash"],
            **record["report"],
        },
    )


@app.get("/api/audits/{audit_id}")
def reopen_audit(audit_id: str) -> JSONResponse:
    rec = _store.get(audit_id)
    if rec is None:
        return JSONResponse(status_code=404, content={"error": "not_found", "audit_id": audit_id})
    return JSONResponse(
        status_code=200,
        content={
            "audit_id": rec["audit_id"],
            "created_at": rec["created_at"],
            "contract_hash": rec["contract_hash"],
            "reused_frozen": True,
            "contract": rec["contract"],
            **rec["report"],
        },
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
