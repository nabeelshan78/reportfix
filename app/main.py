"""FastAPI app: routes + static files. Every endpoint checks permissions on the server."""
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import Session, select

from app import campus, db, permissions, pipeline, workflow
from app.community import trust, voting
from app.models import Flag, Issue, Report, User, iso
from app.triage import llm
from app.triage.locations import list_places
from app.workflow import RuleError

STATIC = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        campus.get()  # validate campus.json at startup: a clear error stops the app
    except campus.CampusError as e:
        raise SystemExit(f"\nCAMPUS MAP ERROR\n{e}\n")
    db.create_tables()
    if not db.schema_is_current():  # an older reportfix.db: rebuild it (seed data only)
        db.reset_all()
    with Session(db.engine) as s:
        if s.exec(select(User)).first() is None:
            from app.seed import seed
            seed(s)
    yield


app = FastAPI(title="ReportFix", lifespan=lifespan)


# Clear messages for users, never a stack trace.
@app.exception_handler(RuleError)
async def rule_error(request: Request, exc: RuleError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(PermissionError)
async def permission_error(request: Request, exc: PermissionError):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def friendly_errors(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={
        "detail": "Something went wrong on the server. Please try again."})


def get_viewer(x_user_id: str | None = Header(default=None),
               session: Session = Depends(db.get_session)) -> User:
    if not x_user_id or not x_user_id.isdigit():
        raise HTTPException(401, "No user selected. Pick a user from the sidebar.")
    user = session.get(User, int(x_user_id))
    if not user:
        raise HTTPException(401, "Unknown user. Pick a user from the sidebar.")
    return user


def require_admin(viewer: User = Depends(get_viewer)) -> User:
    if viewer.role != "admin":
        raise HTTPException(403, "Only the admin can do this.")
    return viewer


def user_payload(session: Session, u: User) -> dict:
    return {"id": u.id, "name": u.name, "role": u.role, "department": u.department,
            "trust": trust.trust_score(session, u.id), "is_seed": u.is_seed,
            "created_at": iso(u.created_at)}


def load_issue(session: Session, issue_id: int, viewer: User) -> Issue:
    issue = session.get(Issue, issue_id)
    if not issue:
        raise HTTPException(404, "Issue not found.")
    if not permissions.can_view_issue(session, issue, viewer):
        raise HTTPException(403, "You are not allowed to view this issue.")
    return issue


def issue_out(session: Session, issue: Issue, viewer: User) -> dict:
    return permissions.serialize_issue(session, issue, viewer, detail=True)


# ---------- judge access ----------

@app.get("/api/health")
def health():
    return llm.provider_status()


@app.get("/api/users")
def list_users(session: Session = Depends(db.get_session)):
    users = session.exec(select(User).order_by(User.id)).all()
    return [user_payload(session, u) for u in users]


@app.post("/api/users/new_student")
def new_student(session: Session = Depends(db.get_session)):
    n = len(session.exec(select(User).where(User.role == "student",
                                            User.is_seed == False)).all()) + 1  # noqa: E712
    u = User(name=f"New Student {n}", role="student")
    session.add(u)
    session.commit()
    session.refresh(u)
    return user_payload(session, u)


@app.post("/api/reset")
def reset():
    db.reset_all()  # llm_cache.db is never touched
    return {"ok": True, "message": "All data reset to the seed state."}


@app.get("/api/me")
def me(viewer: User = Depends(get_viewer), session: Session = Depends(db.get_session)):
    out = user_payload(session, viewer)
    out["weight"] = trust.weight_info(session, viewer)
    return out


@app.get("/api/locations")
def locations():
    return list_places()


# ---------- reports ----------

class ReportIn(BaseModel):
    text: str


class AnswerIn(BaseModel):
    text: str | None = None
    location_id: str | None = None
    problem_type: str | None = None
    choice: str | None = None  # admin_decide | cancel | skip


@app.post("/api/reports")
def create_report(body: ReportIn, viewer: User = Depends(get_viewer),
                  session: Session = Depends(db.get_session)):
    if viewer.role != "student":
        raise HTTPException(403, "Only students can report problems.")
    return pipeline.process_report(session, viewer, body.text)


@app.post("/api/reports/{report_id}/answer")
def answer(report_id: int, body: AnswerIn, viewer: User = Depends(get_viewer),
           session: Session = Depends(db.get_session)):
    return pipeline.answer_report(session, viewer, report_id, body.model_dump())


@app.get("/api/my/pending")
def my_pending(viewer: User = Depends(get_viewer), session: Session = Depends(db.get_session)):
    """The student's own reports that are waiting for an answer (ASK7), incl. optional ones."""
    rows = session.exec(select(Report).where(Report.user_id == viewer.id).order_by(
        Report.id.desc())).all()
    return [pipeline.pending_payload(r) for r in rows
            if r.status == "needs_clarification"
            or (r.status == "confidential" and r.clarification_question)]


# ---------- issues ----------

@app.get("/api/issues")
def issues(viewer: User = Depends(get_viewer), session: Session = Depends(db.get_session)):
    return [permissions.serialize_issue(session, i, viewer)
            for i in permissions.visible_issues(session, viewer)]


@app.get("/api/my/issues")
def my_issues(viewer: User = Depends(get_viewer), session: Session = Depends(db.get_session)):
    return [permissions.serialize_issue(session, i, viewer, detail=True)
            for i in permissions.my_issues(session, viewer)]


@app.get("/api/issues/{issue_id}")
def issue_detail(issue_id: int, viewer: User = Depends(get_viewer),
                 session: Session = Depends(db.get_session)):
    return issue_out(session, load_issue(session, issue_id, viewer), viewer)


class StatusIn(BaseModel):
    status: str
    note: str = ""


class ReasonIn(BaseModel):
    reason: str = ""


class VoteIn(BaseModel):
    value: str


class DeptIn(BaseModel):
    department: str


@app.post("/api/issues/{issue_id}/metoo")
def metoo(issue_id: int, viewer: User = Depends(get_viewer),
          session: Session = Depends(db.get_session)):
    issue = load_issue(session, issue_id, viewer)
    joined, message = workflow.me_too(session, issue, viewer)
    session.commit()
    return {"joined": joined, "message": message, "issue": issue_out(session, issue, viewer)}


@app.post("/api/issues/{issue_id}/status")
def set_status(issue_id: int, body: StatusIn, viewer: User = Depends(get_viewer),
               session: Session = Depends(db.get_session)):
    issue = load_issue(session, issue_id, viewer)
    if not permissions.can_update_issue(session, issue, viewer):
        raise HTTPException(403, "Only staff of this department or the admin can update it.")
    workflow.set_status(session, issue, viewer, body.status, body.note)
    session.commit()
    return issue_out(session, issue, viewer)


@app.post("/api/issues/{issue_id}/mark_fake")
def mark_fake(issue_id: int, body: ReasonIn, viewer: User = Depends(get_viewer),
              session: Session = Depends(db.get_session)):
    issue = load_issue(session, issue_id, viewer)
    if not permissions.can_update_issue(session, issue, viewer):
        raise HTTPException(403, "Only staff of this department or the admin can do this.")
    workflow.mark_fake(session, issue, viewer, body.reason)
    session.commit()
    return issue_out(session, issue, viewer)


@app.post("/api/issues/{issue_id}/vote")
def vote(issue_id: int, body: VoteIn, viewer: User = Depends(get_viewer),
         session: Session = Depends(db.get_session)):
    issue = load_issue(session, issue_id, viewer)
    try:
        voting.cast_vote(session, issue, viewer, body.value)
    except ValueError as e:
        raise RuleError(str(e))
    session.commit()
    return issue_out(session, issue, viewer)


@app.post("/api/issues/{issue_id}/finalize")
def finalize(issue_id: int, admin: User = Depends(require_admin),
             session: Session = Depends(db.get_session)):
    issue = load_issue(session, issue_id, admin)
    try:
        voting.finalize(session, issue, admin)
    except ValueError as e:
        raise RuleError(str(e))
    session.commit()
    return issue_out(session, issue, admin)


@app.post("/api/issues/{issue_id}/reassign")
def reassign(issue_id: int, body: DeptIn, admin: User = Depends(require_admin),
             session: Session = Depends(db.get_session)):
    issue = load_issue(session, issue_id, admin)
    workflow.reassign(session, issue, admin, body.department)
    session.commit()
    return issue_out(session, issue, admin)


@app.post("/api/issues/{issue_id}/lower_safety")
def lower_safety(issue_id: int, body: ReasonIn, admin: User = Depends(require_admin),
                 session: Session = Depends(db.get_session)):
    issue = load_issue(session, issue_id, admin)
    workflow.lower_safety(session, issue, admin, body.reason)
    session.commit()
    return issue_out(session, issue, admin)


# ---------- trust + flags ----------

@app.get("/api/users/{user_id}/trust")
def user_trust(user_id: int, viewer: User = Depends(get_viewer),
               session: Session = Depends(db.get_session)):
    if viewer.id != user_id and viewer.role != "admin":
        raise HTTPException(403, "You can only see your own trust ledger.")
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    events = trust.ledger(session, user_id)
    return {"user": user_payload(session, target),
            "weight": trust.weight_info(session, target),
            "base": 50,
            "ledger": [{"code": e.code, "delta": e.delta, "reason": e.reason_text,
                        "issue_id": e.issue_id, "at": iso(e.created_at)} for e in events]}


@app.get("/api/admin/flags")
def flags(admin: User = Depends(require_admin), session: Session = Depends(db.get_session)):
    out = []
    for f in session.exec(select(Flag).order_by(Flag.id.desc())):
        names = []
        for uid in json.loads(f.user_ids_json):
            u = session.get(User, uid)
            names.append(u.name if u else f"user {uid}")
        out.append({"id": f.id, "type": f.type, "detail": f.detail, "issue_id": f.issue_id,
                    "users": names, "cleared": f.cleared, "created_at": iso(f.created_at)})
    return out


@app.get("/api/admin/waiting")
def admin_waiting(admin: User = Depends(require_admin),
                  session: Session = Depends(db.get_session)):
    out = []
    for r in permissions.waiting_reports(session):
        student = session.get(User, r.user_id)
        out.append({**pipeline.pending_payload(r), "student": student.name if student else "?",
                    "created_at": iso(r.created_at), "decision_log": json.loads(r.decision_log_json)})
    return out


@app.get("/api/admin/emergencies")
def admin_emergencies(admin: User = Depends(require_admin),
                      session: Session = Depends(db.get_session)):
    return [permissions.serialize_issue(session, i, admin, detail=True)
            for i in permissions.emergency_issues(session)]


@app.post("/api/admin/flags/{flag_id}/clear")
def clear_flag(flag_id: int, admin: User = Depends(require_admin),
               session: Session = Depends(db.get_session)):
    f = session.get(Flag, flag_id)
    if not f:
        raise HTTPException(404, "Flag not found.")
    f.cleared = True
    session.add(f)
    issue = session.get(Issue, f.issue_id) if f.issue_id else None
    if issue:
        from app.community import priority
        priority.recompute_priority(session, issue)
    session.commit()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
