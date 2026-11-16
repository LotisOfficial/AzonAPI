# How the API reads and writes PONT and PINS files

Written 16 November 2026 from the source (`essential_open_api/jvm.py`, `routes.py`, `repo_switch.py`) and from running the API in Docker against copies of two repositories. Statements about Protégé's internal behaviour that were not observed directly are marked **(assumed)**.

## Summary

The API never opens a `.pont` or `.pins` file itself. It runs the Protégé 3.5 engine (`protege.jar`) inside its own process and asks that engine to load a project, answer questions about it, change it and save it. The files are read once at load and rewritten in full at every save.

```
HTTP request
   │
Flask route (routes.py)             Python
   │   calls methods on Java objects through JPype
   ▼
Protégé KnowledgeBase / Project     Java, same process (in-process JVM)
   │   loadProjectFromFile() at load; project.save() at every write
   ▼
<name>.pprj ──names──► <name>.pont (classes and slots)
                       <name>.pins (instances)
```

## The three files

| File | Holds | Read by the API | Written by the API |
|---|---|---|---|
| `.pprj` | The Protégé project: which `.pont` and `.pins` files belong to it, and the form layout for every class. About 37 MB, nearly all of it form layout. | At load, by Protégé | At every save, by Protégé (`project.save`) **(assumed to rewrite all three; the `.pins` change was observed)** |
| `.pont` | Class and slot definitions in CLIPS syntax: `defclass` blocks, with slot type, cardinality, allowed classes and inverse slot. | At load | At every save; unchanged in content because the API does not edit classes or slots |
| `.pins` | Instances: `([Id] of Class (slot value) ...)`. Links to other instances are `[OtherId]`. | At load | At every save |

The API is given a `.pprj` path and nothing else. Protégé finds the other two through the names stored inside the `.pprj`.

## Loading

`jvm.py`:

1. `start_jvm()` starts one JVM in the Python process, with every JAR in `jars/` on the classpath.
2. `Project.loadProjectFromFile(path, [])` parses all three files and builds the whole repository in memory. For the repositories tested this took 30 to 50 seconds and printed "Loaded N instances" lines to the log.
3. `project.getKnowledgeBase()` returns the object every route uses.

Which project loads at start-up: the path remembered in `config/active.json` if that file exists, otherwise `PPRJ_FILE`.

Switching repository (`repo_switch.py`) repeats step 2 on a background thread and swaps the active project when it is ready. Up to `REPO_CACHE_SIZE` projects (default 2) stay in memory; a cached one is reused only if the modification time and size of its `.pprj`, `.pont` and `.pins` files are unchanged. Two repositories in memory used about 2.7 GiB.

## Reading

Every read is a call on the in-memory knowledge base. Nothing touches the disk.

| Endpoint | Protégé calls |
|---|---|
| `GET /api/classes/` | `getRootCls()`, then `getDirectSubclasses()`, `isAbstract()` and `getDirectInstances()` down the tree |
| `GET /api/classes/{c}/` | `getCls(c)`, `getDirectSubclasses()` |
| `GET /api/classes/{c}/slots` | `getCls(c).getTemplateSlots()`; for each slot its value type, cardinality and `getAllowedClses()` |
| `GET /api/classes/{c}/instances` | `getDirectInstances()` or `getInstances()` (with subclasses), then the requested slots of each |
| `GET /api/instances/{id}` | `getInstance(id)`, then `getOwnSlotValues(slot)` for each filled slot; linked instances are followed to `maxdepth` |
| `GET /api/classes/{c}/form` | **Not Protégé.** Reads `resources/forms/{c}.json` |

The form files are not read from the `.pprj` at run time. `util/create_form_specs_for_api.py` extracted them once from a baseline project into `resources/forms/` (565 files). A form changed in Protégé afterwards does not reach the editor until that script is run again. Slots with no place in a form still appear, because the page adds them from `/slots`.

## Writing

Create (`POST /api/instances`), update (`PATCH /api/instances/{id}`) and delete (`DELETE /api/instances/{id}`) all follow the same shape:

1. Change the in-memory knowledge base: `kb.createInstance(None, cls)`, `setOwnSlotValue` / `setOwnSlotValues` / `addOwnSlotValue`, or the delete call.
2. Call `project.save(errors)`. Protégé rewrites the project's files in full.
3. If the save reports errors, undo the in-memory change (delete created frames, restore the slot values that were snapshotted before the change) and return an error.

Details that matter:

- **Instance ids are assigned by Protégé.** `createInstance(None, cls)` produces the next `<ProjectName>_Class<N>` name, for example `A230Clean_Class20000`.
- **The name is a slot, not the id.** The API looks for the first of `name_`, `name`, `relation_name`, `:relation_name` that the class has.
- **A second instance of the same class with the same name is refused** (409).
- **Inverse slots are maintained by Protégé**, not by the API: setting one side updates the other in memory, and both are saved **(assumed from Protégé's documented behaviour; not separately tested here)**.
- **Type, cardinality and allowed-class rules are applied by Protégé** as far as it enforces them; the API adds little validation of its own.
- **The whole project is saved for every change.** Cost grows with repository size, not with the size of the edit.
- **There is no lock.** Two requests that change the knowledge base at the same time are not serialised by the API (finding H5; plan item B3).
- **There is no backup and no history.** The previous contents of the files are gone after a save.
- After a successful save the API tells the repository cache that the files changed because it wrote them, so the in-memory copy is still treated as current.

## Publishing

`POST /api/publish` passes the in-memory project to `PublishService.startPublishAsync(project, url, user, pwd)` in `publish-service-1.0.0.jar`. That JAR builds the Viewer's XML snapshot from the project and uploads it to the Viewer. The API reads back status and log text with `getPublishStatus` and `getPublishLogs`. The `.pont` and `.pins` files are not involved; the snapshot comes from memory.

## What follows from this design

- The files are always a complete, consistent Protégé project, so Protégé 3.5 can open the same repository at any time. Do not edit a repository in Protégé while the API has it loaded: each side overwrites the other on save.
- A change made to the files outside the API is not seen until the repository is reloaded. Switching away and back reloads it if the files' time or size changed.
- One API process only. The loaded repositories are in that process's memory.
- Everything Protégé-specific is behind `jvm.py` and the helper functions in `routes.py`. Replacing the engine with one that parses and writes the two text files directly, while keeping the endpoints, is the subject of section 5 of the Admin repository's review document and group E of its plan.

## Points to check against other notes

These are the places where this description relies on Protégé behaviour rather than on the API's own code, and so are the most worth confirming:

1. Whether `project.save()` rewrites the `.pprj` and `.pont` every time or only the files that changed.
2. That inverse slot values are stored on both instances in the `.pins` file.
3. How `createInstance(None, cls)` chooses the next number, and whether it can collide after instances are deleted. (In testing, the id `A230Clean_Class20000` was issued again after the first instance with that id had been deleted.)
4. Whether included projects, if a repository uses them, are saved to their own files.
