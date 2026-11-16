# Bulk import: how it works today and how to rebuild it without Protégé 3.5

**Status:** analysis and design only. Nothing in this document has been built.

**Basis:** one complete import set inspected on 16 Nov 2026 (`update.info`, `updatepack.xsd`, `standardFunctions.py` v2.9, and a 91,261-line `dup_import_script.py`), plus the contents listing of a second `.dup` package. Counts below are from that one script. Statements that could not be confirmed from the files are marked **VERIFY**.

## 1. What the bulk import set is

A `.dup` file ("data update pack") is a ZIP archive of exactly four files:

| File | Size in the sample | Purpose |
|---|---|---|
| `update.info` | 1 KB | XML manifest: pack name, description, and the ordered list of scripts to run |
| `updatepack.xsd` | 3 KB | XML schema for `update.info` |
| `standardFunctions.py` | 61 KB | Library of 47 helper functions, written by the Essential Project (GPL v3) |
| `dup_import_script.py` | 2.9 MB (11 MB in a later pack) | The data: a generated script that calls the helper functions once per spreadsheet cell |

The Essential Import Utility generates the pack from an Excel workbook and an import specification. The pack is then applied inside Protégé 3.5 through the Essential Update tab (named in the schema's header comment), which runs the scripts against the open repository.

### `update.info`

```xml
<updatepack xmlns="http://www.enterprise-architecture.org/essential/updatepack" ...>
    <updatename>Azon230 Import Data Import - 20251220061520</updatename>
    <updatedescription>Azon 230 Import Utility</updatedescription>
    <pack name="Common Functions" sequence="0">
        <filename>standardFunctions.py</filename>
    </pack>
    <pack name="Azon230 Import Data Import - 20251220061520" sequence="1">
        <filename>dup_import_script.py</filename>
        <chunktoken>###-CHUNK-###</chunktoken>
    </pack>
</updatepack>
```

Packs run in `sequence` order: first the function library, then the data script. `chunktoken` is an optional marker for splitting a large script into pieces.

### `standardFunctions.py`

Python 2 syntax (`print "..."`), importing Java classes (`from java.util import Date`, `from edu.stanford.smi.protege.model import ValueType`), and using a global `kb` that it never defines. It is therefore run by a Python-on-Java interpreter (Jython) inside Protégé, with `kb` supplied as the open knowledge base. **VERIFY** the interpreter and version in the Essential Update tab plug-in.

An older copy (v2.2.1) sits unused in this repository at `essential_open_api/standardFunctions.py`; nothing imports it.

### `dup_import_script.py`

Despite being a Python file, the sample contains no logic at all: no `if`, no loops, no function definitions. It is a flat list of operations, grouped by worksheet:

```python
defineExternalRepository("Essential Launchpad", "")

# Script for Worksheet: Business Capabilities #
BusDom=EssentialGetInstance("Business_Domain", None, u"Banking", None, None)
aBusCap=EssentialGetInstance("Business_Capability", None, u"Banking", u"BC1", u"Essential Launchpad")
addIfNotThere(aBusCap, "description", u"Banking services including ...")
addIfNotThere(aBusCap, "belongs_to_business_domain", BusDom)
aBusCap.setOwnSlotValue(kb.getSlot("business_capability_index"), Integer("1"))
####_End_of_Node_####
```

Measured vocabulary of the whole script:

| Statement | Count | Notes |
|---|---|---|
| `var = EssentialGetInstance(class, None, name, extId, extRepo)` | 8,275 | Instance id is always `None`. 951 calls give an external id and repository; 7,324 give a name only. |
| `addIfNotThere(var, "slot", value)` | 8,893 | Value is another variable (about 7,900) or a string literal. |
| `var.setOwnSlotValue(kb.getSlot("slot"), Integer("n"))` | 692 | Direct Protégé call, bypassing the library. |
| `var.setOwnSlotValue(kb.getSlot("slot"), Boolean("..."))` | 115 | Both `"FALSE"` and `"false"` occur. |
| `defineExternalRepository(name, description)` | 1 | |
| `####_End_of_Node_####` marker | 55,194 | Comment line; ends the output for one spreadsheet row or node. |
| `###-CHUNK-###` (the declared chunk token) | 0 | Declared in the manifest but absent from this script. |

Also: 38 distinct classes, 56 distinct slots, 77 variable names (reused constantly, so order matters), 26 worksheets, and 254 statements with leading whitespace even though they are not inside any block.

## 2. What the operations mean

These rules are read from `standardFunctions.py` v2.9. A replacement must reproduce them exactly, including the quirks.

### `EssentialGetInstance(class, instanceId, name, externalId, externalRepo)` — find or create

Tried in this order; the first hit wins:

1. **Unknown class** → print a warning, return `None`. Every later operation on that variable is silently skipped.
2. **By instance id** (frame name), if given. On a hit: update or add the external reference, and **rename** the instance to `name` if `name` is not empty.
3. **By external reference.** Find `External_Instance_Reference` instances whose `external_instance_reference` equals the id and whose `external_repository_reference` is the named repository; follow `referenced_instance` to the target. The target must be of exactly the requested class, otherwise warn and treat as not found. An external reference pointing at nothing is deleted. On a hit: refresh the reference's `external_update_date` and rename as in step 2.
4. **By name.** Exact, case-sensitive match on the class's name slot, over **direct** instances of the class only (subclasses are not searched), first match wins. On a hit: add or refresh the external reference. The name is not changed.
5. **Create.** New instance of the class with a Protégé-generated id; set the name slot; if an external id was given, create an `External_Instance_Reference` named `"<repo>::<id>"` with the repository, id and a timestamp, and attach it through `external_repository_instance_reference`.

Names, ids and repository names are stripped of leading and trailing spaces. The name slot is `name`, `relation_name` or `:relation_name`, whichever the class has.

### `addIfNotThere(instance, slot, value)`

- Instance is `None` → do nothing.
- Slot does not exist → print a warning, do nothing.
- Slot allows multiple values → add the value unless an equal value is already there.
- Slot allows one value → **overwrite** whatever is there.

### Direct `setOwnSlotValue`

Replaces the slot's value with a typed value. Java's `Boolean(String)` is true only for the text `true` in any letter case; `Boolean("1")` is false. `Integer("n")` parses a decimal integer.

### `defineExternalRepository(name, description)`

Find the `External_Repository` with that name, or create it.

### Effects that come from Protégé, not from the script

- Setting a slot that has an inverse updates the other end automatically (**VERIFY**; see PONT_PINS_LOADING.md).
- New instances get a generated frame name.
- Nothing is written to disk until the user saves the project in Protégé.

## 3. Where the dependency on Protégé 3.5 lies

| Dependency | Why it exists |
|---|---|
| Protégé desktop must be open with the repository loaded | The Update tab is a Protégé plug-in |
| Jython inside the Java 8 process | The pack is executable Python 2 that imports Java classes |
| `kb` and the Protégé Java API | Every helper reads and writes through it |
| A person at the desktop | Open project, load pack, run, check console output, save |

There is also a safety problem independent of Protégé: **a `.dup` pack is code.** Whoever can supply a pack can run anything on the machine that applies it, with that user's rights. The pack even carries its own copy of the function library, which the tab runs without checking.

## 4. Rebuild design

### Principle: treat the pack as data, never as code

The data script uses five statement shapes and no logic. So do not run it; **parse** it into a list of operations and apply those with our own engine. This removes Jython, removes the executable-pack risk, and makes the import independent of how the repository is stored.

### Components

```
.dup  ──►  1. Unpacker  ──►  2. Script parser  ──►  3. Operation list  ──►  4. Import engine  ──►  5. Store adapter
           (zip checks)      (strict grammar)       (JSON lines)            (the rules in §2)       (Protégé today,
                                                                                   │                 Python or DB later)
                                                                                   └──► 6. Report (dry run or applied)
```

**1. Unpacker.** Accept a ZIP containing only the four expected file names, with size limits, rejecting any entry with a path. Read `update.info` for the name, description and script file names. Ignore the pack's `standardFunctions.py` entirely except to record its version line; warn if it is a version the engine has not been checked against, because the rules in section 2 may have changed.

**2. Script parser.** Strip leading whitespace from each line (safe because the script has no blocks), drop comments and the Java `import` lines, then parse each remaining statement with Python's `ast` module and accept only these shapes:

| Shape | Becomes |
|---|---|
| `NAME = EssentialGetInstance(str, None-or-str, str-or-None, str-or-None, str-or-None)` | `get` |
| `addIfNotThere(NAME, str, NAME-or-literal)` | `add` |
| `NAME.setOwnSlotValue(kb.getSlot(str), Integer(str) / Boolean(str) / Float(str) / Double(str))` | `set` |
| `defineExternalRepository(str, str)` | `repo` |

Anything else rejects the whole pack with the line number. Nothing is ever passed to `exec` or `eval`. **VERIFY** the grammar against more packs first: the library also offers delete and remove functions (`EssentialDeleteInstance`, `removeInstanceFromSlot`, `deleteAllValuesFromSlot`), `GetActorToRole`, `addClassToSlot` and `addSlotToSlot`, which other import specifications may emit.

**3. Operation list.** One JSON object per line, in script order, for example:

```json
{"op":"get","var":"aBusCap","class":"Business_Capability","name":"Banking","extId":"BC1","extRepo":"Essential Launchpad","line":31}
{"op":"add","var":"aBusCap","slot":"belongs_to_business_domain","ref":"BusDom","line":36}
{"op":"set","var":"aBusCap","slot":"business_capability_index","type":"Integer","value":"1","line":38}
```

This intermediate file is the stable contract. It can be stored, diffed, reviewed, and later produced directly from Excel without generating a script at all.

**4. Import engine.** Implements section 2 against an abstract store. Differences from the original, all invisible in the result:

- **Indexes instead of scans.** The original searches every direct instance of a class on each lookup (8,275 lookups here). Build a `(class, name) → instance` map and a `(repository, external id) → instance` map once, and keep them current as instances are created or renamed.
- **One transaction.** Apply every operation in memory, then save once. On any failure, discard by reloading from disk.
- **Dry run.** Run the same code against the store without saving and return the report only.
- **Variables.** A dictionary; a later assignment to the same name replaces the earlier one, as in the script.

**5. Store adapter.** The engine needs only: look up a class and a slot (multiple or single, value type); list direct instances of a class; get an instance by id; create and delete an instance; read, set and add slot values. Two adapters:

- **Protégé adapter (first).** Calls the knowledge base the API already holds. Protégé still maintains inverse slots and generates ids. This removes Protégé desktop, Jython and the manual steps, but Java is still in the API.
- **Native adapter (later).** The pure-Python model or database described in [JAVA_OPTIONS.md](JAVA_OPTIONS.md), options C and D. At that point the import has no Protégé dependency at all. This adapter must itself maintain inverse slots and generate ids.

**6. Report.** Per class: created, matched by external reference, matched by name, renamed; slots changed; every warning with its script line (unknown class, unknown slot, external reference used by another class). This replaces reading Protégé's console.

### How it is exposed

- `POST /api/imports` — admin role, upload a `.dup`; optional `dryRun=true`. Returns a job id. Needs its own body-size limit (the general limit is 1 MB; recent packs contain an 11 MB script).
- `GET /api/imports/<id>` — status, progress, and the report.
- Runs under the knowledge-base lock from plan phase 3 so that API writes and an import cannot interleave; takes a backup of the three repository files before applying.
- A command-line entry point (`python -m essential_open_api.importer pack.dup --dry-run`) using the same engine, for use without the API running.

### Proving it is equivalent

1. Take two identical copies of a repository.
2. Apply a pack to copy A with Protégé's Update tab and save.
3. Apply the same pack to copy B with the new engine.
4. Compare the two `.pins` files after normalising the two things that legitimately differ: generated frame names of newly created instances, and `external_update_date` timestamps.
5. Repeat with a pack applied **twice** (the import must be repeatable without creating duplicates), and with a pack that renames instances found by external reference.

The comparison tool is worth building first; the existing PONT/PINS converters can supply the parsing.

### Stages

| Stage | Delivers | Protégé 3.5 still needed for |
|---|---|---|
| 1 | Unpacker, parser, operation list, dry-run report from the command line | applying the import |
| 2 | Engine with the Protégé adapter; `POST /api/imports`; equivalence test passing | nothing at the desktop; `protege.jar` inside the API |
| 3 | Native adapter on the Python model or database | nothing |
| 4 (optional) | Generate the operation list straight from Excel and the import specification, retiring the script format and the Essential Import Utility | nothing |

Stage 1 has no dependency on the rest of the security plan and can start at once. Stage 2 depends on plan phase 3 (lock, backups). Stage 3 depends on the Java-removal decision.

## 5. Open questions

- **VERIFY** how the Update tab executes the script: whole file, per chunk token, or per `####_End_of_Node_####` marker. It affects only how faithfully error handling must be copied (does one bad statement stop the pack, the chunk, or nothing?).
- **VERIFY** why 254 statements are indented and whether the Update tab tolerates that by design.
- **VERIFY** inverse-slot behaviour by experiment before writing the native adapter.
- Survey more packs to fix the grammar; one sample is not enough.
- Decide whether an import that matches by name should be allowed to match across subclasses. Today it does not; changing that would change results.
