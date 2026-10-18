"""Bind the durable finalization state machine to a Flask application."""

from __future__ import annotations

from pathlib import Path

from .destination_plugin_service import installed_destination_definitions
from .finalization_service import FinalizationService
from .upload_job_destination_handler import UploadJobDestinationHandler


def configure_finalization(app) -> FinalizationService:
    upload_jobs = app.config.get("UPLOAD_JOBS_SERVICE")
    destination_handler = (
        UploadJobDestinationHandler(upload_jobs, Path(app.config["DATA_DIR"]))
        if upload_jobs is not None
        else None
    )
    service = FinalizationService(
        Path(app.config["DATA_DIR"]),
        recording_adapter=app.config.get("FINALIZATION_RECORDING_ADAPTER"),
        destination_handler=app.config.get("FINALIZATION_DESTINATION_HANDLER") or destination_handler,
        destination_definitions=(
            app.config.get("FINALIZATION_DESTINATION_DEFINITIONS")
            or installed_destination_definitions()
        ),
        card_summary_builder=app.config.get("CARD_SUMMARY_BUILDER"),
    )
    app.config["FINALIZATION_SERVICE"] = service
    return service
