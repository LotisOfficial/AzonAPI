# How the API reads Protégé PPRJ, PONT and PINS files

**Short version:** the API contains no parser of its own. Python starts a Java virtual machine inside its own process, asks the Protégé 3.5 library (`jars/protege.jar`) to load the project, and then works entirely on the in-memory knowledge base that Protégé builds. Every write is followed by Protégé rewriting all three files in full.

Sources for this document: the code in `essential_open_api/`, the class list inside `jars/protege.jar`, and one real repository inspected on 16 Nov 2026 (883 classes, 24,608 instances). Statements about Protégé internals that were not confirmed from those sources are marked *(Protégé behaviour, not verified here)*.

## The three files

| File | Role | Format | Size in the inspected repository |
|---|---|---|---|
| `<name>.pprj` | Project file: where the other two files are, plus every form layout and UI setting | CLIPS instances | 37 MB |
| `<store>.pont` | Ontology: classes and slot definitions (the metamodel) | CLIPS `defclass` | 1.7 MB |
| `<store>.pins` | Instances: the repository content | CLIPS instances | 14 MB |

All three are plain text. The inspected files were ASCII with CRLF line endings and start with a header:

```
; Sun Nov 15 18:49:59 ACDT 2026
;
;+ (version "3.5")
;+ (build "Build 663")
```

Lines starting `;+` are comments to a CLIPS interpreter but carry meaning for Protégé (see PONT below).

### PPRJ: the pointer to the other two

The `.pprj` is itself a small knowledge base saved as CLIPS instances. Most of it is form layout (`Widget`, `Property_List` instances), which is why it is the largest file. The part that matters for loading is three `String` instances:

```
([KB_351967_Instance_0] of  String
	(name "factory_class_name")
	(string_value "edu.stanford.smi.protege.storage.clips.ClipsKnowledgeBaseFactory"))

([KB_351967_Instance_78] of  String
	(name "classes_file_name")
	(string_value "store_51.pont"))

([KB_351967_Instance_79] of  String
	(name "instances_file_name")
	(string_value "store_51.pins"))
```

- `factory_class_name` says which storage back end to use. `ClipsKnowledgeBaseFactory` is the text-file back end.
- `classes_file_name` and `instances_file_name` are bare file names, resolved relative to the folder holding the `.pprj` *(Protégé behaviour, not verified here)*. This is why a repository folder must be moved or copied as a whole.

The project instance also sets the default metaclasses (`:ESSENTIAL-CLASS`, `:ESSENTIAL-SLOT`), which is how Essential adds its own attributes to classes and slots.

### PONT: classes and slots

```
(defclass %3ACLIPS_TOP_LEVEL_SLOT_CLASS "Fake class to save top-level slot information"
	(is-a USER)
	(role abstract)
	(multislot provides_application_services
;+		(comment "Specifies the Application Services that are provided by an Application Provider")
		(type INSTANCE)
;+		(allowed-classes Application_Provider_Role)
;+		(inverse-slot role_for_application_provider)
		(create-accessor read-write))
	(single-slot sq_index
		(type INTEGER)
;+		(cardinality 0 1)
		(create-accessor read-write))
```

- Each `defclass` gives the class name, parent (`is-a`), whether it is abstract (`role`), and its slots.
- `single-slot` holds one value, `multislot` many.
- The `;+` lines hold what CLIPS cannot express: `allowed-classes`, `cardinality`, `inverse-slot`, `comment`. Protégé reads them; a plain CLIPS tool would ignore them.
- `%3A` is a URL-encoded colon. `%3ACLIPS_TOP_LEVEL_SLOT_CLASS` is `:CLIPS_TOP_LEVEL_SLOT_CLASS`, an artificial first class that Protégé uses to store every slot's top-level definition. System frames start with a colon.
- The inspected file has 763 `inverse-slot` declarations. They matter for writes (see below).

### PINS: instances

```
([%3AAFA-RELATION] of  %3AESSENTIAL-CLASS

	(essential_id "cl1"))
```

- Each block is `([frame name] of Class (slot value …) …)`.
- The frame name in square brackets is the instance's permanent id. It is what the API returns as `id` and accepts in `/api/instances/<id>`.
- A value in square brackets is a reference to another instance; quoted values are strings; bare values are numbers or symbols such as `TRUE`.
- The file starts with instances of the metaclass `:ESSENTIAL-CLASS`, one per class, so classes carry their own attribute values (for example `essential_id`).

## The load sequence

```
waitress → wsgi.py → create_app() → jvm.load_pprj()
                                      ├─ start_jvm()            JPype starts a JVM in this process, classpath = jars/*.jar
                                      ├─ _resolve_active_pprj() which .pprj: saved repository id, else PPRJ_FILE
                                      └─ _open_project(path)
                                           Project.loadProjectFromFile(path, errors)   ← Protégé does everything here
                                           project.getKnowledgeBase()
```

1. **Configuration** (`jvm.py`, read once at import): `JARS_DIR`, `PPRJ_FILE`, `REPO_ROOT`, `CONFIG_FILE`.
2. **JVM start.** `jpype.startJVM(classpath=[every jar in jars/])`. The JVM lives inside the Python process; there is no separate Java service. `JAVA_HOME` selects the runtime.
3. **Choosing the project.** If `CONFIG_FILE` holds a repository id that still exists under `REPO_ROOT`, that one; otherwise `PPRJ_FILE`.
4. **Loading.** One Java call: `edu.stanford.smi.protege.model.Project.loadProjectFromFile(path, errors)`. Inside it, Protégé:
   1. parses the `.pprj` into a project knowledge base;
   2. reads `factory_class_name` and instantiates `ClipsKnowledgeBaseFactory`;
   3. the factory parses the `.pont` to create classes, slots and facets, then the `.pins` to create instances and fill their slot values *(order is Protégé behaviour, not verified here)*;
   4. returns a `Project` whose `getKnowledgeBase()` is the complete model in memory.
5. **Errors.** Protégé reports parse problems by adding them to the `errors` collection instead of throwing. The upstream code passed a Python list, which JPype copies, so those messages were silently lost. This fork passes a real `java.util.ArrayList` and logs each entry.
6. **Result.** Two module-level globals, `_PROTEGE_PROJECT` and `_KNOWLEDGE_BASE`, shared by every request thread.

Consequences:

- The whole repository is in memory for the life of the process. Nothing is read lazily and the files are not watched: a change made on disk by Protégé desktop is invisible until the API restarts or the repository is switched.
- Start-up time and memory grow with repository size. The 37 MB project file is parsed even though the API never uses the form layouts in it.
- `resources/forms/*.json` are **not** read from the `.pprj`. They are static files generated ahead of time by `util/create_form_specs_for_api.py` and served by `/api/classes/<class>/form`.
- Other files in a repository folder (for example `config.eip`) are not referenced by the `.pprj` and are not read.

## How requests use the loaded model

No request touches the files to read. Handlers in `routes.py` call Protégé's Java API through JPype:

| API operation | Protégé calls used |
|---|---|
| Class tree | `kb.getCls`, `cls.getDirectSubclasses`, `getSubclasses`, `isAbstract` |
| Slots of a class | `cls.getTemplateSlots`, `slot.getValueType`, `getAllowedClses`, `getTemplateSlotMinimumCardinality` / `MaximumCardinality` |
| Instances of a class | `cls.getInstances` (includes subclasses) or `getDirectInstances` |
| One instance | `kb.getInstance(frameName)`, `getDirectType`, `getOwnSlots`, `getOwnSlotValues`, `getBrowserText` |
| Create | `kb.createInstance(None, cls)` (Protégé generates the frame name), then `setOwnSlotValue` / `addOwnSlotValue` |
| Update | `setOwnSlotValues`, `addOwnSlotValue`, `removeOwnSlotValue`, `setBrowserText` |
| Delete | `kb.deleteInstance(inst)` |

JSON shaping (`frame_to_dict`) walks referenced instances to `maxdepth`, skips slots whose names start with `:`, and returns each instance as `id` (frame name), `name` (browser text) and `className`.

Because writes go through Protégé's own API, Protégé keeps the model consistent: when a slot with an `inverse-slot` is set, the other side is updated too *(Protégé behaviour, not verified here)*. Anything that replaces Protégé must reproduce this.

## How writes reach the files

After every successful create, update or delete the handler calls `jvm.save_project()`, which calls `project.save(errors)`. Protégé then writes the **entire** `.pprj`, `.pont` and `.pins` again from memory (classes `ClipsFileWriter`, `ClsStorer`, `InstanceStorer` in `protege.jar`).

- There is no incremental or append write. A one-field change rewrites about 53 MB in the inspected repository.
- There is no file locking. If Protégé desktop has the same repository open and saves, one side overwrites the other's changes.
- If a save fails, the handler removes the instances it created and restores changed slot values in memory, so memory and disk stay aligned.
- Requests are not yet serialised against each other (plan phase 3). Two simultaneous writes can interleave.

## Switching repository

`POST /api/repository` with a listed id calls `jvm.reload_pprj()`: the new project is loaded with the same `loadProjectFromFile` call, the globals are swapped, the id is saved to `CONFIG_FILE`, and the previous project is disposed. Only `.pprj` files found directly in `REPO_ROOT` or one folder below it can be selected.

## Publishing

Publishing does not read the files either. `PublishService` (in `jars/publish-service-1.0.0.jar`) takes the in-memory `Project`, renders it to Essential report XML with `XMLRenderer` (`essential-xml` jar), and posts it to `<viewer url>/reportService` with `ReportServiceClient` (`essential-widgets` jar), on a background Java thread.

## What depends on Java

| Capability | Java component |
|---|---|
| Parsing `.pprj`, `.pont`, `.pins` | `protege.jar` |
| In-memory model, inverse slots, browser text, frame-name generation | `protege.jar` |
| Writing the three files | `protege.jar` |
| Report XML for the Viewer | `essential-xml`, `essential-widgets`, `publish-service` jars |
| Bridge from Python | JPype |

[JAVA_OPTIONS.md](JAVA_OPTIONS.md) uses this table to lay out what removing Java would involve.
