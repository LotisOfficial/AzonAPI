"""JVM bootstrap and Protege project loading for the AZON API.

See docs/PONT_PINS_LOADING.md for how the .pprj/.pont/.pins files are read.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

import jpype

log = logging.getLogger("azon.jvm")

# Configuration - environment variables, with defaults relative to the project root.
BASE_DIR = Path(__file__).resolve().parent.parent

JARS_DIR = Path(os.environ.get("JARS_DIR", BASE_DIR / "jars"))
PPRJ_DIR = Path(os.environ.get("PPRJ_DIR", BASE_DIR / "resources"))

env_pprj_file = os.environ.get("PPRJ_FILE")
if env_pprj_file:
    PPRJ_FILE = Path(env_pprj_file)
else:
    PPRJ_FILE = PPRJ_DIR / "essential_baseline_6_20.pprj"

# Only projects under REPO_ROOT can be selected through the API.
REPO_ROOT = Path(os.environ.get("REPO_ROOT", PPRJ_DIR))

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", BASE_DIR / "config" / "active.json"))

# Collect all jar files placed in the jars directory.
CLASSPATH = [str(jar.resolve()) for jar in sorted(JARS_DIR.glob("*.jar"))]

_PROTEGE_PROJECT: Optional[object] = None
_KNOWLEDGE_BASE: Optional[object] = None
_ACTIVE_PPRJ_PATH: Optional[Path] = None


# ----- Repository discovery -------------------------------------------------

def _scan_repositories() -> list:
    """Find .pprj files in REPO_ROOT and one level below it.

    Returns a list of (id, name, file, path). The id is the path relative to
    REPO_ROOT with forward slashes, e.g. ``Arthur/Arthur.pprj``; it is the only
    handle API callers ever see or send.
    """
    found = []
    if not REPO_ROOT.is_dir():
        return found
    root = REPO_ROOT.resolve()
    for item in sorted(REPO_ROOT.iterdir()):
        if item.is_dir():
            candidates = [(item.name, pprj) for pprj in sorted(item.glob("*.pprj"))]
        elif item.suffix.lower() == ".pprj":
            candidates = [(item.stem, item)]
        else:
            continue
        for name, pprj in candidates:
            resolved = pprj.resolve()
            # Skip links that point outside the repository root.
            if not resolved.is_file() or not resolved.is_relative_to(root):
                continue
            found.append((resolved.relative_to(root).as_posix(), name, pprj.name, resolved))
    return found


def resolve_repository_id(repo_id) -> Optional[Path]:
    """Map a repository id to its path, or None if it is not a listed repository.

    The id must exactly equal one produced by a fresh scan, so traversal
    sequences, absolute paths and unknown names can never resolve.
    """
    if not isinstance(repo_id, str) or not repo_id:
        return None
    for found_id, _name, _file, path in _scan_repositories():
        if found_id == repo_id:
            return path
    return None


def _describe(path: Optional[Path]) -> Tuple[Optional[str], Optional[str]]:
    """Return (id, name) for a project path; id is None outside REPO_ROOT."""
    if path is None:
        return None, None
    resolved = path.resolve()
    for found_id, name, _file, found_path in _scan_repositories():
        if found_path == resolved:
            return found_id, name
    return None, path.stem  # active project set by PPRJ_FILE outside REPO_ROOT


def list_repositories() -> list:
    """Repositories available for switching. Never includes filesystem paths."""
    active = _ACTIVE_PPRJ_PATH.resolve() if _ACTIVE_PPRJ_PATH is not None else None
    return [
        {"id": repo_id, "name": name, "file": file_name, "active": path == active}
        for repo_id, name, file_name, path in _scan_repositories()
    ]


def get_current_repository() -> dict:
    """Describe the active repository without exposing filesystem paths."""
    path = _ACTIVE_PPRJ_PATH
    repo_id, name = _describe(path)
    return {
        "id": repo_id,
        "name": name,
        "file": path.name if path else None,
        "loaded": _KNOWLEDGE_BASE is not None,
    }


def _resolve_active_pprj() -> Path:
    """Active project: the repository id saved in CONFIG_FILE, else PPRJ_FILE."""
    if CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("Ignoring unreadable config file %s", CONFIG_FILE)
            data = {}
        saved = data.get("repository") if isinstance(data, dict) else None
        if saved:
            path = resolve_repository_id(saved)
            if path is not None:
                return path
            log.warning("Saved repository %r is not under REPO_ROOT; using PPRJ_FILE.", saved)
    return PPRJ_FILE


def _save_active_repository(repo_id: str) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"repository": repo_id}, indent=2), encoding="utf-8")


# ----- JVM and project loading ---------------------------------------------

_JVM_START_FAILED = False


def start_jvm() -> bool:
    """Ensure the JVM is running with the configured classpath.

    Returns True when the JVM is available. A failed start is remembered and
    never retried: a second jpype.startJVM() call after a failure crashes the
    process. JPype 1.7 needs Java 9 or later (use Java 17).
    """
    global _JVM_START_FAILED  # noqa: PLW0603

    # Check the failure flag first: after a failed start JPype can still report
    # the JVM as started, and using it then crashes the process.
    if _JVM_START_FAILED:
        return False
    if jpype.isJVMStarted():
        return True

    if not CLASSPATH:
        log.error("No .jar files found in %s. Add them before starting the JVM.", JARS_DIR)
        _JVM_START_FAILED = True
        return False

    log.info("Starting JVM...")
    try:
        jpype.startJVM(classpath=CLASSPATH)
    except Exception:  # pylint: disable=broad-except
        _JVM_START_FAILED = True
        log.exception("JVM failed to start; knowledge base endpoints will be unavailable")
        return False
    log.info("JVM started.")
    return True


def _open_project(path: Path):
    """Load a Protege project and return (project, knowledge_base).

    Project.loadProjectFromFile reads the .pprj, which names the .pont (classes)
    and .pins (instances) files to parse. Parse problems are collected in the
    errors list rather than raised, so they are logged here.
    """
    Project = jpype.JClass("edu.stanford.smi.protege.model.Project")
    errors = jpype.JClass("java.util.ArrayList")()
    project = Project.loadProjectFromFile(str(path), errors)
    if not errors.isEmpty():
        for err in errors:
            log.warning("Protege load message: %s", err)
    return project, project.getKnowledgeBase()


def load_pprj() -> Optional[Tuple[object, object]]:
    """Load the active .pprj file and return the project and Knowledge Base."""
    global _PROTEGE_PROJECT, _KNOWLEDGE_BASE, _ACTIVE_PPRJ_PATH  # noqa: PLW0603

    try:
        if _KNOWLEDGE_BASE is not None:
            return _PROTEGE_PROJECT, _KNOWLEDGE_BASE

        if not start_jvm():
            return None

        active_path = _resolve_active_pprj()
        if not active_path.is_file():
            log.error("Project file not found: %s", active_path)
            return None

        log.info("Loading Protege project: %s", active_path)
        project, kb = _open_project(active_path)
        log.info("Knowledge Base loaded.")
        _PROTEGE_PROJECT, _KNOWLEDGE_BASE, _ACTIVE_PPRJ_PATH = project, kb, active_path
        return project, kb

    except Exception:  # pylint: disable=broad-except
        log.exception("Error while loading project")
        return None


def reload_pprj(repo_id: str) -> Tuple[bool, str]:
    """Switch the knowledge base to another repository under REPO_ROOT.

    Returns (success, message). Messages are safe to return to API callers.
    """
    global _PROTEGE_PROJECT, _KNOWLEDGE_BASE, _ACTIVE_PPRJ_PATH  # noqa: PLW0603

    path = resolve_repository_id(repo_id)
    if path is None:
        return False, "Unknown repository."

    if not start_jvm():
        return False, "Failed to load repository."

    try:
        log.info("Switching to Protege project: %s", path)
        project, kb = _open_project(path)
    except Exception:  # pylint: disable=broad-except
        log.exception("Error while loading project %s", path)
        return False, "Failed to load repository."

    previous = _PROTEGE_PROJECT
    _PROTEGE_PROJECT, _KNOWLEDGE_BASE, _ACTIVE_PPRJ_PATH = project, kb, path
    _save_active_repository(repo_id)

    if previous is not None:
        try:
            previous.dispose()
        except Exception:  # pylint: disable=broad-except
            log.debug("Could not dispose previous project", exc_info=True)

    return True, "Repository loaded."


def get_knowledge_base() -> Optional[object]:
    """Return the loaded Knowledge Base, if available."""
    return _KNOWLEDGE_BASE


def get_project() -> Optional[object]:
    """Return the loaded Protege project, if available."""
    return _PROTEGE_PROJECT


# ----- Publishing ------------------------------------------------------------

_PUBLISH_SERVICE = "com.enterprise_architecture.essential_os.publish_service.PublishService"


def call_publish_async(project: object, url: str, user: str, pwd: str) -> Optional[str]:
    """Trigger asynchronous publishing via PublishService."""
    if not start_jvm():
        return None
    try:
        publish_service = jpype.JClass(_PUBLISH_SERVICE)
        return str(publish_service.startPublishAsync(project, url, user, pwd))
    except Exception:  # pylint: disable=broad-except
        log.exception("Error while starting publish job")
        return None


def get_publish_status(job_id: str) -> Optional[Tuple[str, str]]:
    """Retrieve the status and logs of a publish job."""
    if not start_jvm():
        return None
    try:
        publish_service = jpype.JClass(_PUBLISH_SERVICE)
        return str(publish_service.getPublishStatus(job_id)), str(publish_service.getPublishLogs(job_id))
    except Exception:  # pylint: disable=broad-except
        log.exception("Error while checking publish status")
        return None


# ----- Saving ----------------------------------------------------------------

def save_project():
    """Persist the current Protege project to disk.

    Returns (success flag, error messages if any). Rewrites the .pprj, .pont
    and .pins files in full.
    """
    project = get_project()
    if project is None:
        log.error("Cannot save project: Protege project not loaded.")
        return False, ["Protege project not loaded."]

    try:
        errors = jpype.JClass("java.util.ArrayList")()
        project.save(errors)

        if not errors.isEmpty():
            for err in errors:
                log.error("Protege save error: %s", err)
            return False, ["Failed to save the repository."]

        log.info("Protege project saved.")
        return True, None
    except Exception:  # pylint: disable=broad-except
        log.exception("Error while saving project")
        return False, ["Failed to save the repository."]
