"""WSGI entry point for the AZON API.

Run with a single process (the knowledge base lives in one in-process JVM):

    waitress-serve --listen=127.0.0.1:5200 --threads=8 wsgi:app
"""

from essential_open_api import create_app

app = create_app()

__all__ = ["app"]
