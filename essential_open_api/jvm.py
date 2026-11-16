import os
import time
from pathlib import Path
from typing import Optional, Tuple

import jpype

from .repo_switch import RepositorySwitcher

# Configuration - Prefer environment variables, fallback to defaults relative to project root
BASE_DIR = Path(__file__).resolve().parent.parent

JARS_DIR = Path(os.environ.get("JARS_DIR", BASE_DIR / "jars"))
PPRJ_DIR = Path(os.environ.get("PPRJ_DIR", BASE_DIR / "resources"))

env_pprj_file = os.environ.get("PPRJ_FILE")
if env_pprj_file:
    PPRJ_FILE = Path(env_pprj_file)
else:
    PPRJ_FILE = PPRJ_DIR / "essential_baseline_6_20.pprj"

REPO_ROOT = Path(os.environ.get("REPO_ROOT", PPRJ_DIR))

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", BASE_DIR / "config" / "active.json"))

JARS_DIR.mkdir(parents=True, exist_ok=True)
PPRJ_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Collect all jar files placed in the jars directory.
CLASSPATH = [str(jar.resolve()) for jar in JARS_DIR.glob("*.jar")]


def _resolve_active_pprj() -> Path:
    """Return the active .pprj path: config file takes priority over env var."""
    if CONFIG_FILE.exists():
        try:
            import json
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            saved = data.get("pprj_file")
            if saved and Path(saved).exists():
                return Path(saved)
        except Exception:
            pass
    return PPRJ_FILE


def _save_active_pprj(path: Path) -> None:
    """Persist the active .pprj path to the config file."""
    import json
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps({"pprj_file": str(path)}, indent=2),
        encoding="utf-8"
    )


def start_jvm() -> None:
    """Ensure the JVM is running with the configured classpath."""
    if jpype.isJVMStarted():
        return

    if not CLASSPATH:
        print(f"No .jar files found in {JARS_DIR}. Add them before starting the JVM.")
        return

    print("Starting JVM...")
    jpype.startJVM(classpath=CLASSPATH)
    print("JVM started successfully!")


_PROTEGE_PROJECT: Optional[object] = None
_KNOWLEDGE_BASE: Optional[object] = None
_ACTIVE_PPRJ_PATH: Optional[Path] = None


def _load_project(path: str) -> Tuple[object, object]:
    """Load a .pprj and return (project, knowledge base). Raises on failure. Runs on a worker thread."""
    start_jvm()
    project_class = jpype.JPackage("edu.stanford.smi.protege.model").Project
    print(f"Loading Protege project: {path}...")
    project = project_class.loadProjectFromFile(str(path), [])
    return project, project.getKnowledgeBase()


def _activate(path: str, project: object, kb: object) -> None:
    """Make a loaded repository the one every request uses, and remember it for the next start."""
    global _PROTEGE_PROJECT, _KNOWLEDGE_BASE, _ACTIVE_PPRJ_PATH  # noqa: PLW0603
    _PROTEGE_PROJECT, _KNOWLEDGE_BASE, _ACTIVE_PPRJ_PATH = project, kb, Path(path)
    _save_active_pprj(Path(path))
    print(f"Active repository: {path}")


def _release(project: object) -> None:
    """Free a repository dropped from the cache."""
    dispose = getattr(project, "dispose", None)
    if callable(dispose):
        dispose()


# Switches run in the background; recently used repositories stay loaded (REPO_CACHE_SIZE, default 2).
SWITCHER = RepositorySwitcher(
    loader=_load_project,
    on_loaded=_activate,
    on_evict=_release,
    cache_size=int(os.environ.get("REPO_CACHE_SIZE", "2") or 2),
)


def load_pprj() -> Optional[Tuple[object, object]]:
    """Load the .pprj file and return the project and Knowledge Base."""
    global _PROTEGE_PROJECT, _KNOWLEDGE_BASE, _ACTIVE_PPRJ_PATH  # noqa: PLW0603

    try:
        if _KNOWLEDGE_BASE is not None:
            return _PROTEGE_PROJECT, _KNOWLEDGE_BASE

        start_jvm()

        active_path = _resolve_active_pprj()

        if not active_path.exists():
            print(f"Error: The file {active_path} was not found!")
            return None

        protege_package = jpype.JPackage("edu.stanford.smi.protege.model")
        project_class = protege_package.Project

        print(f"Loading Protege project: {active_path}...")
        began = time.monotonic()
        project = project_class.loadProjectFromFile(str(active_path), [])

        kb = project.getKnowledgeBase()
        print("Knowledge Base loaded successfully!")
        _PROTEGE_PROJECT, _KNOWLEDGE_BASE, _ACTIVE_PPRJ_PATH = project, kb, active_path
        # Keep it in the switcher's cache, and use its load time to estimate later switches.
        SWITCHER.prime(str(active_path), project, kb, time.monotonic() - began)
        return project, kb

    except Exception as exc:  # pylint: disable=broad-except
        print(f"Error while loading project: {exc}")
        return None


def reload_pprj(new_path: str) -> Tuple[bool, str]:
    """Reload the knowledge base from a new .pprj file path.

    Returns (success, message).
    """
    global _PROTEGE_PROJECT, _KNOWLEDGE_BASE, _ACTIVE_PPRJ_PATH  # noqa: PLW0603

    path = Path(new_path)

    if not path.exists():
        return False, f"File not found: {new_path}"

    if path.suffix.lower() != ".pprj":
        return False, f"File must be a .pprj file: {new_path}"

    try:
        start_jvm()

        protege_package = jpype.JPackage("edu.stanford.smi.protege.model")
        project_class = protege_package.Project

        print(f"Reloading Protege project: {path}...")
        project = project_class.loadProjectFromFile(str(path), [])
        kb = project.getKnowledgeBase()

        _PROTEGE_PROJECT = project
        _KNOWLEDGE_BASE = kb
        _ACTIVE_PPRJ_PATH = path

        _save_active_pprj(path)

        print(f"Knowledge Base reloaded successfully from {path}")
        return True, f"Knowledge Base loaded from {path}"

    except Exception as exc:  # pylint: disable=broad-except
        print(f"Error while reloading project: {exc}")
        return False, f"Failed to load project: {exc}"


def get_current_pprj_path() -> Optional[str]:
    """Return the full path of the currently loaded .pprj file."""
    if _ACTIVE_PPRJ_PATH is not None:
        return str(_ACTIVE_PPRJ_PATH)
    active = _resolve_active_pprj()
    return str(active)


def list_repositories() -> list:
    """Scan REPO_ROOT one level deep for .pprj files."""
    results = []
    if not REPO_ROOT.exists():
        return results
    for item in sorted(REPO_ROOT.iterdir()):
        if item.is_dir():
            for pprj in sorted(item.glob("*.pprj")):
                results.append({
                    "name": item.name,
                    "file": pprj.name,
                    "path": str(pprj),
                    "active": _ACTIVE_PPRJ_PATH is not None and pprj.resolve() == _ACTIVE_PPRJ_PATH.resolve()
                })
        elif item.suffix.lower() == ".pprj":
            results.append({
                "name": item.stem,
                "file": item.name,
                "path": str(item),
                "active": _ACTIVE_PPRJ_PATH is not None and item.resolve() == _ACTIVE_PPRJ_PATH.resolve()
            })
    return results


def get_knowledge_base() -> Optional[object]:
    """Return the loaded Knowledge Base, if available."""
    return _KNOWLEDGE_BASE


def get_project() -> Optional[object]:
    """Return the loaded Protege project, if available."""
    return _PROTEGE_PROJECT


def call_publish_async(project: object, url: str, user: str, pwd: str) -> Optional[str]:
    """Trigger asynchronous publishing via PublishService."""
    try:
        start_jvm()
        publish_service = jpype.JClass("com.enterprise_architecture.essential_os.publish_service.PublishService")
        job_id = publish_service.startPublishAsync(project, url, user, pwd)
        return str(job_id)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Error while starting publish job: {exc}")
        return None


def get_publish_status(job_id: str) -> Optional[Tuple[str, str]]:
    """Retrieve the status and logs of a publish job."""
    try:
        start_jvm()
        publish_service = jpype.JClass("com.enterprise_architecture.essential_os.publish_service.PublishService")
        status = publish_service.getPublishStatus(job_id)
        logs = publish_service.getPublishLogs(job_id)
        return str(status), str(logs)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Error while checking publish status: {exc}")
        return None


def save_project():
    """Persist the current Protege project to disk."""
    project = get_project()
    if project is None:
        print("Cannot save project: Protege project not loaded.")
        return False, ["Protege project not loaded."]

    try:
        start_jvm()
        ArrayList = jpype.JClass("java.util.ArrayList")
        errors = ArrayList()

        save_method = getattr(project, "save", None)
        if callable(save_method):
            project.save(errors)
        else:
            legacy_save = getattr(project, "saveProject", None)
            if callable(legacy_save):
                legacy_save()
            else:
                print("Project object does not expose a save/saveProject method.")
                return False, ["Project object does not expose a save/saveProject method."]

        if hasattr(errors, "isEmpty") and not errors.isEmpty():
            error_messages = [str(err) for err in errors]
            print(f"Errors while saving project: {error_messages}")
            return False, error_messages

        print("Protege project saved successfully.")
        # The files changed because we wrote them; the loaded copy is still current.
        SWITCHER.refresh(str(_ACTIVE_PPRJ_PATH) if _ACTIVE_PPRJ_PATH else None)
        return True, None
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Error while saving project: {exc}")
        return False, [f"Error while saving project: {exc}"]
