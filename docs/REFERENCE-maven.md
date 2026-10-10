# Reference build: Maven

Supporting context for migrating **from** Maven. The migration process itself
lives in [SKILL.md](../SKILL.md); this file only covers what is unique to Maven
as the reference build.

**Status: early** — argv-floor only. Treat output as exploratory.

## Detecting it

A `pom.xml` at the repo root.

## Scope traps

Not yet supported:
- Coordinate-identity deps (`group:artifact:version`, scope) — not extracted.
- Java flag canonicalization.

The C/C++ `any2bazel.json` levers and the fix table in SKILL.md don't apply to
Java source sets.

## Extracting the reference model

Maven has no action graph; the reference is the **forked `javac` argument
file**.
```bash
mvn clean compile -Dmaven.compiler.fork=true          # writes <module>/target/*arguments
python3 "$SKILL_DIR/scripts/extract_maven.py" <module_dir> <repo_root> model.maven.json
```

## Extracting the Bazel action graph

Use the `Javac` mnemonic in place of the C/C++ mnemonic set:
```bash
bazel aquery 'mnemonic("Javac", //...)' \
    --features=-compiler_param_file --features=-linker_param_file \
    --output=jsonproto > aquery.json
```

## Comparison and diff kinds

Java compiles a whole source set at once, so it's compared as a project-wide
union of `.java` sources (grouping/naming agnostic), analogous to how libraries
are compared.

| kind | meaning |
|------|---------|
| `missing_java_src` | a `.java` compiled by Maven but not Bazel — add it to a `java_library`'s `srcs` |
| `extra_java_src`   | a `.java` compiled by Bazel but not Maven |
