import queue

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from . import __version__
from .jobs import Job, new_job_id
from .testpage import test_blocks
from .validation import ValidationError, validate_document, validate_text_request

MAX_CONTENT_LENGTH = 8_000_000  # bounds base64 image decode cost on a 512MB Pi


def create_app(config, store, work_queue, printer_manager):
    app = Flask("printapi")
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

    def enqueue(blocks, source):
        job = Job(id=new_job_id(), blocks=blocks, source=source)
        store.add(job)
        try:
            work_queue.put_nowait(job.id)
        except queue.Full:
            store.remove(job.id)
            return jsonify({"error": "queue full"}), 503
        return (
            jsonify({"id": job.id, "status": job.status, "status_url": f"/jobs/{job.id}"}),
            202,
        )

    def json_body():
        payload = request.get_json(silent=True)
        if payload is None:
            raise ValidationError("", "request body must be valid JSON")
        return payload

    @app.post("/print")
    def print_document():
        return enqueue(validate_document(json_body()), source="print")

    @app.post("/print/text")
    def print_text():
        return enqueue(validate_text_request(json_body()), source="text")

    @app.post("/print/test")
    def print_test():
        return enqueue(validate_document({"blocks": test_blocks()}), source="test")

    @app.get("/jobs/<job_id>")
    def get_job(job_id):
        job = store.get(job_id)
        if job is None:
            return jsonify({"error": "job not found"}), 404
        return jsonify(job)

    @app.get("/jobs")
    def list_jobs():
        return jsonify({"jobs": store.recent(config.history_max)})

    @app.get("/status")
    def status():
        return jsonify(
            {
                "printer": printer_manager.status(),
                "queue": {"depth": work_queue.qsize(), "current_job": store.current_job_id()},
            }
        )

    @app.get("/health")
    @app.get("/")
    def health():
        return jsonify({"ok": True, "version": __version__})

    @app.errorhandler(ValidationError)
    def handle_validation_error(e):
        return jsonify({"error": str(e)}), 400

    @app.errorhandler(HTTPException)
    def handle_http_exception(e):
        return jsonify({"error": e.description}), e.code

    @app.errorhandler(Exception)
    def handle_unexpected(e):
        app.logger.exception("unhandled error")
        return jsonify({"error": f"internal error: {type(e).__name__}"}), 500

    return app
