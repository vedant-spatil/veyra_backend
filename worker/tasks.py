from app.db import SessionLocal
from app.dograh import DograhError, dograh
from app.models import Call
from app.usage import bump_usage
from worker.celery_app import celery_app


@celery_app.task(bind=True, max_retries=3, default_retry_delay=15)
def retry_dial(self, call_id: str):
    db = SessionLocal()
    try:
        call = db.get(Call, call_id)
        if call is None or call.dograh_run_id:
            return {"skipped": True}
        try:
            result = dograh.initiate_call(call.to_number, call.variables or {})
        except DograhError as exc:
            call.status = "failed"
            call.error = str(exc)[:500]
            db.commit()
            raise self.retry(exc=exc)
        call.dograh_run_id = result["run_id"]
        call.status = "dialing"
        call.error = ""
        bump_usage(db, call.tenant_id, "calls", 1)
        db.commit()
        return {"run_id": result["run_id"]}
    finally:
        db.close()
