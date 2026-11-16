"""Factory function for the Essential Open API Flask application."""

# Imports are inside create_app() so that tools such as `python -m essential_open_api.users_cli`
# can import this package without starting the JVM or loading a repository.


def create_app():
    """Create and configure a Flask application instance."""
    from flask import Flask
    from flasgger import Swagger

    from .auth import init_auth
    from .jvm import get_knowledge_base, load_pprj
    from .routes import api_bp

    app = Flask(__name__)

    # Sign-in, the deny-by-default check, security headers and (optionally) the Admin page itself.
    # Runs first so that a bad configuration stops start-up before the repository is loaded.
    # Controlled by AZON_AUTH_MODE=auth|off. There is deliberately no CORS: the Admin page is
    # served from this same origin (AZON_ADMIN_STATIC_DIR) or proxied to it.
    init_auth(app)

    # Configure Flasgger
    swagger_template = {
        "swagger": "2.0",
        "info": {
            "title": "Essential Open API",
            "description": "API for accessing the Protégé knowledge base.",
            "version": "1.0.0"
        },
        "basePath": "/"
    }
    swagger_config = {
        "headers": [],
        "specs": [
            {
                "endpoint": 'apispec',
                "route": '/apispec.json',
                "rule_filter": lambda rule: True,
                "model_filter": lambda tag: True,
            }
        ],
        "static_url_path": "/flasgger_static",
        "swagger_ui": True,
        "specs_route": "/api/"
    }
    Swagger(app, template=swagger_template, config=swagger_config)

    load_pprj()

    app.register_blueprint(api_bp, url_prefix="/api")

    @app.get("/health")
    def health() -> dict[str, str]:
        """Simple health-check endpoint.
        ---
        tags:
          - System
        description: Returns the health status of the API and whether the knowledge base is loaded.
        responses:
          200:
            description: Health status object
            schema:
              type: object
              properties:
                status:
                  type: string
                  example: ok
                kb_loaded:
                  type: boolean
                  example: true
        """
        kb_loaded = get_knowledge_base() is not None
        return {"status": "ok", "kb_loaded": kb_loaded}

    return app


_app = None


def __getattr__(name):
    """Build the application the first time `essential_open_api.app` is asked for (wsgi.py does this)."""
    global _app  # noqa: PLW0603
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
