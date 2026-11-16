# Java: staying on 8, migrating, or removing it

**Decision (16 Nov 2026):** the stack stays on Java 8 for now, because Protégé 3.5 and the rest of the Essential tooling are on Java 8. This document records what that costs, how to move later, and how Java could be removed.

## 1. Where Java is used

Only inside the API process, through JPype, for four things: parsing the repository files, holding the in-memory model, writing the files back, and rendering report XML for the Viewer. Details are in [PONT_PINS_LOADING.md](PONT_PINS_LOADING.md).

## 2. Staying on Java 8 (current position)

Java 8 fixes two other version choices:

| Component | Constraint | Pinned to |
|---|---|---|
| JPype | 1.7.1 refuses to start a Java 8 JVM: `Java version too old. Java 9 or later is required` (observed here on 16 Nov 2026). | `jpype1==1.5.2` |
| Python | `jpype1` 1.5.x and 1.6.0 have no prebuilt package for Python 3.14 (checked with `pip download` here). | Python 3.12 |

**VERIFY before relying on the pins:** that `jpype1==1.5.2` starts the Java 8 runtime and loads a repository on Python 3.12. This could not be tested during the first build session because the only Python on the build machine was 3.14. `jpype1==1.6.0` may also still accept Java 8; test it if 1.5.2 gives trouble.

Costs of staying:

- Java 8 and JPype 1.5 receive no new features and limited fixes; Python 3.12 leaves security support in October 2028.
- Use a vendor build of Java 8 that still gets security updates (Temurin, Zulu, Corretto) and keep it patched.
- The API should never be exposed beyond loopback or a reverse proxy; that limits what an unpatched runtime can be reached with.

One observation worth knowing: the upstream Docker image this project started from does **not** run Java 8. Its Dockerfile installs Debian's `default-jre` on a `python:3.11-slim` base, which is Java 17 or later, and its `JAVA_HOME` line points at a Java 11 path that does not exist in that image. If that container has been serving a repository, then Protégé 3.5's loader has already been running on a newer Java there. That is evidence the migration below is feasible, not proof; it was not re-tested here.

## 3. Migration path to a newer Java (17 LTS)

Only the API process changes. Protégé desktop and Tomcat can stay on Java 8, because the API and the desktop tool share files, not a runtime.

1. **Build a test rig.** Python 3.12 venv, `jpype1` latest, Java 17, pointed at a *copy* of a production repository.
2. **Read test.** Load the copy; compare `GET /api/classes/` and a full dump of `GET /api/classes/<class>/instances` for the main classes against the same calls on the Java 8 build. They must match exactly.
3. **Write round-trip test.** Create, update and delete instances; save; then open the saved files in Protégé desktop on Java 8 and confirm they load with no errors and that a re-save from Protégé produces no unexpected differences. This is the test that matters most: both runtimes must write files the other can read.
4. **Character test.** Include instances with non-ASCII text (accents, dashes, currency symbols). Default file encoding changed to UTF-8 in Java 18; on Java 17 it still follows the operating system. Set `-Dfile.encoding` explicitly so both runtimes agree, and record the value.
5. **Publish test.** Publish to a test Viewer and compare the rendered report XML with one produced on Java 8.
6. **Soak.** Run the Java 17 build against a copy for a working week of normal use before switching the live repository.
7. **Switch.** Change `JAVA_HOME` (native) or the Java stage of the Dockerfile; unpin `jpype1` and Python in `requirements.txt`. Keep the Java 8 build available for rollback; the file format is unchanged, so rollback is only a restart.

Known risk areas to watch in steps 2 to 5: strong encapsulation of JDK internals (reflection warnings or failures from old libraries), removed Java EE modules (`javax.xml.bind` and similar) if any jar uses them, and default-encoding differences.

## 4. Options for removing Java

Each option below is judged against the four Java-dependent capabilities.

### Option A — Take Java out of the Python process (sidecar)

Keep Protégé, but run it as a small separate Java 8 service that the Python API calls over loopback HTTP.

- **Removes:** JPype, and with it the link between the Java version and the Python version. Python can move to the current release immediately.
- **Keeps:** Java 8, `protege.jar`, everything else.
- **Effort:** small to medium. The Java service is a thin wrapper over calls already listed in PONT_PINS_LOADING.md.
- **Use when:** the Python pin is the pressing problem and the file format must stay byte-for-byte Protégé's.

This is isolation, not elimination.

### Option B — Pure-Python reader, Java only for writing and publishing

Parse `.pont` and `.pins` in Python into an in-memory model and serve all `GET` endpoints from it. Lotis already has Python converters that read and write these formats with verified round-trip fidelity (`pont_json_converter.py`, `manifestinstance_converter.py`); they are the natural starting point.

- **Removes:** Java from the read path, which is most of the traffic, and from start-up (no 37 MB `.pprj` parse; only the three pointer values are needed from it).
- **Keeps:** Java for create, update, delete and publish.
- **Effort:** medium. Needs class inheritance of slots, subclass-inclusive instance listing, and browser text (the display name rule per class).
- **Risk:** two models of the same data in one service; the Python copy must be refreshed after every Java write.
- **Use when:** as the first stage of Option C, not as an end state.

### Option C — Pure-Python reader and writer (Java removed from the API)

Extend Option B so Python also applies changes and writes `.pont` and `.pins` back.

- **Removes:** `protege.jar` and JPype from the API.
- **Must reproduce from Protégé:**
  - inverse-slot maintenance (763 inverse declarations in the inspected ontology);
  - frame-name generation for new instances, without colliding with names Protégé will generate later;
  - cardinality and allowed-class checks;
  - output that Protégé 3.5 desktop re-opens cleanly, if modellers still use it. The `.pprj` can be left untouched as long as the class and instance file names do not change.
- **Still needs an answer for publishing:** either a Python implementation of the Essential report XML (`XMLRenderer`), which the Viewer's XSL depends on in detail, or keeping a small Java publish tool that is run only at publish time.
- **Effort:** large. The report XML renderer is the single biggest piece and the hardest to prove correct.
- **Use when:** Protégé desktop remains the modelling tool but the API must be free of Java.

### Option D — Database as the source of truth

Import the repository into a database (SQLite for single-user, PostgreSQL for shared use); the API reads and writes the database; `.pont`/`.pins` and report XML become export formats.

- **Removes:** Java, the whole-file rewrite on every change, and the last-writer-wins problem with concurrent edits.
- **Adds:** real transactions, history and audit, concurrent users.
- **Costs:** Protégé desktop stops being a live editor; it could only work on exports, and changes made there would need a controlled import. Publishing still needs the report XML renderer from Option C.
- **Effort:** largest.
- **Use when:** Azon Admin is intended to replace Protégé desktop as the modelling tool.

### Comparison

| | Java left | Python unpinned | Protégé desktop still a live editor | Publish needs new renderer | Effort |
|---|---|---|---|---|---|
| Stay on Java 8 | all | no | yes | no | none |
| Migrate to Java 17 | all (newer) | yes | yes | no | small |
| A. Sidecar | all (separate process) | yes | yes | no | small–medium |
| B. Python reads | writes + publish | no | yes | no | medium |
| C. Python reads and writes | publish only, or none | yes | yes, with care | yes, for none | large |
| D. Database | none | yes | no | yes | largest |

## 5. Recommendation

1. **Now:** stay on Java 8 with the pins in section 2, and finish the security plan. Nothing in the security work depends on the Java version.
2. **Next, cheaply:** run the migration tests in section 3 on a copy. If they pass, moving the API alone to Java 17 removes the JPype and Python pins with no code change, and nothing else in the stack has to move.
3. **If Java must go:** build Option B on the existing converters, then extend to Option C, treating the report XML renderer as its own project with the Viewer's output as the acceptance test. Choose Option D instead only if the decision is made to retire Protégé desktop.
