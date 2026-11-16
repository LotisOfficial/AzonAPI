"""Factory function for the AZON API Flask application."""

import logging
import os

from flask import Flask

from . import jvm
from .auth import load_tokens
from .targets import load_targets


def create_app() -> Flask:
    """Create and configure a Flask application instance.

    Raises ``AuthConfigError`` / ``TargetConfigError`` when the security
    configuration is missing or malformed, so the process never serves
    requests without authentication.
    """
    logging.basicConfig(
        level=os.environ.get("AZON_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = Flask(__name__)

    # Fail closed: no token file, no API.
    app.config["AZON_TOKENS"] = load_tokens()
    app.config["AZON_VIEWER_TARGETS"] = load_targets()
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("AZON_MAX_BODY_BYTES", 1024 * 1024))

    # Interactive API docs are opt-in and meant for development only: the
    # Swagger UI pages themselves are served without authentication.
    if os.environ.get("AZON_API_DOCS") == "1":
        from flasgger import Swagger  # dev dependency

        Swagger(
            app,
            template={
                "swagger": "2.0",
                "info": {
                    "title": "AZON API",
                    "description": "API for accessing the Protege knowledge base.",
                    "version": "1.0.0",
                },
                "basePath": "/",
                "securityDefinitions": {
                    "Bearer": {"type": "apiKey", "name": "Authorization", "in": "header"}
                },
                "security": [{"Bearer": []}],
            },
            config={
                "headers": [],
                "specs": [
                    {
                        "endpoint": "apispec",
                        "route": "/apispec.json",
                        "rule_filter": lambda rule: True,
                        "model_filter": lambda tag: True,
                    }
                ],
                "static_url_path": "/flasgger_static",
                "swagger_ui": True,
                "specs_route": "/api/",
            },
        )

    # No CORS: the Admin UI is served from the same origin as the API.

    jvm.load_pprj()

    from .routes import api_bp

    app.register_blueprint(api_bp, url_prefix="/api")

    @app.get("/health")
    def health() -> dict:
        """Unauthenticated liveness probe. Reveals nothing but load state."""
        return {"status": "ok", "kb_loaded": jvm.get_knowledge_base() is not None}

    return app
