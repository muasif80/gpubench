"""Read ``run-schema.json``, and validate a run bundle against it, with the standard library only.

Why this file exists
--------------------
``run-schema.json`` is 1,600 lines of JSON Schema 2020-12 describing what a run bundle must
contain and where every number lives. Until now nothing in the tool could read it, so the schema
was a document rather than a check, and README section 9 lists that as known gap G1.

The template is standard library only by contract (see ``template/__init__.py``), so ``jsonschema``
is not available and never will be. This module implements the subset of JSON Schema the manifest
actually uses, and REFUSES TO RUN over a schema that uses anything else.

That refusal is the whole design. A validator that quietly ignores a keyword it does not implement
is the defect class this template exists to prevent: it reports "valid" without having looked, and
the keyword an author most wants enforced is exactly the one a partial validator is most likely to
skip. So :func:`unsupported_keywords` walks every schema node before any instance is touched, and
the CLI exits non-zero naming what it found. An unimplemented keyword is a bug to fix here, not a
silent pass over there.

Implemented, because ``run-schema.json`` uses them
-------------------------------------------------
``type`` (including a list of types), ``properties``, ``required``, ``additionalProperties``
(boolean and schema), ``patternProperties``, ``items``, ``$ref`` (JSON pointer into this
document), ``$defs``, ``enum``, ``const``, ``minItems``, ``maxItems``, ``uniqueItems``,
``minLength``, ``maxLength``, ``minimum``, ``maximum``, ``exclusiveMinimum``,
``exclusiveMaximum``, ``multipleOf``, ``pattern``, ``allOf``, ``anyOf``, ``oneOf``, ``not``,
``if``/``then``/``else``, ``contains`` with ``minContains`` and ``maxContains``,
``propertyNames``, and ``format`` for the two formats the schema uses (``date`` and
``date-time``).

Carried but not enforced, because JSON Schema defines them as annotations and nothing about an
instance can contradict them: ``$schema``, ``$id``, ``title``, ``description``, ``$comment``,
``default``, ``examples``, ``deprecated``, ``readOnly``, ``writeOnly``.

Deliberately NOT implemented: ``dependentSchemas``, ``dependentRequired``, ``prefixItems``,
``unevaluatedProperties``, ``unevaluatedItems``, remote ``$ref`` to another document, and
``$dynamicRef``. None appears in ``run-schema.json``; if one is added, the support check fails
loudly and this module has to grow before the schema ships.
"""

from __future__ import annotations

import json
import os
import re

__all__ = [
    "SchemaError",
    "default_schema_path",
    "load_schema",
    "unsupported_keywords",
    "validate",
    "summarise",
]


class SchemaError(Exception):
    """The schema itself cannot be used: unreadable, or outside the implemented subset."""


# Keywords that constrain an instance and are implemented below. Anything outside this set and
# ANNOTATION_KEYWORDS makes the support check fail rather than be skipped.
ASSERTION_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "patternProperties", "items",
    "$ref", "$defs", "definitions", "enum", "const", "minItems", "maxItems", "uniqueItems",
    "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "pattern", "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "format",
    "contains", "minContains", "maxContains", "propertyNames",
})

# Keywords JSON Schema defines as annotations: they describe, they do not constrain. Listing them
# explicitly is what keeps the support check honest, because "not an assertion" is a claim that has
# to be made once, in the open, rather than inferred every time an unknown key turns up.
ANNOTATION_KEYWORDS = frozenset({
    "$schema", "$id", "$anchor", "title", "description", "$comment", "default", "examples",
    "deprecated", "readOnly", "writeOnly",
})

# The formats that carry a real check here. A format outside this set is unsupported rather than
# ignored: `format` is an annotation by specification, but an author who writes it expects it to
# bite, and a validator that silently agrees to nothing is worse than one that says it cannot.
_FORMATS = {
    "date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "date-time": re.compile(
        r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"),
}

# Keyword -> how its value holds subschemas, so the support walk visits schema nodes and nothing
# else. Without this the walk would descend into `properties` and read every property NAME as a
# keyword, which is how a "strict" checker ends up rejecting a schema for containing a field
# called `type`.
_SUBSCHEMA = "schema"
_SUBSCHEMA_MAP = "map"
_SUBSCHEMA_LIST = "list"
_CHILD_KEYWORDS = {
    "items": _SUBSCHEMA, "not": _SUBSCHEMA, "if": _SUBSCHEMA, "then": _SUBSCHEMA,
    "else": _SUBSCHEMA, "additionalProperties": _SUBSCHEMA,
    "contains": _SUBSCHEMA, "propertyNames": _SUBSCHEMA,
    "properties": _SUBSCHEMA_MAP, "patternProperties": _SUBSCHEMA_MAP,
    "$defs": _SUBSCHEMA_MAP, "definitions": _SUBSCHEMA_MAP,
    "allOf": _SUBSCHEMA_LIST, "anyOf": _SUBSCHEMA_LIST, "oneOf": _SUBSCHEMA_LIST,
}


def default_schema_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-schema.json")


def load_schema(path=None):
    """Load the schema, or raise SchemaError naming what went wrong."""
    path = path or default_schema_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (IOError, OSError) as exc:
        raise SchemaError("cannot read schema %s: %s" % (path, exc))
    except ValueError as exc:
        raise SchemaError("schema %s is not valid JSON: %s" % (path, exc))
    if not isinstance(doc, dict):
        raise SchemaError("schema %s holds a %s, not an object" % (path, type(doc).__name__))
    return doc


def _iter_schema_nodes(node, pointer="#"):
    """Yield (pointer, schema_object) for every schema node reachable from ``node``."""
    if isinstance(node, bool):
        return
    if not isinstance(node, dict):
        return
    yield pointer, node
    for key, value in node.items():
        how = _CHILD_KEYWORDS.get(key)
        if how is None:
            continue
        if how == _SUBSCHEMA:
            for item in _iter_schema_nodes(value, "%s/%s" % (pointer, key)):
                yield item
        elif how == _SUBSCHEMA_MAP and isinstance(value, dict):
            for name, sub in value.items():
                for item in _iter_schema_nodes(sub, "%s/%s/%s" % (pointer, key, name)):
                    yield item
        elif how == _SUBSCHEMA_LIST and isinstance(value, list):
            for i, sub in enumerate(value):
                for item in _iter_schema_nodes(sub, "%s/%s/%d" % (pointer, key, i)):
                    yield item


def unsupported_keywords(schema):
    """Every keyword in the schema this module does not implement, as (pointer, keyword) pairs.

    Empty means the whole schema is enforced. Non-empty means validation must not be attempted:
    the caller has to report the gap rather than return a verdict that skipped it.
    """
    found = []
    for pointer, node in _iter_schema_nodes(schema):
        for key in sorted(node):
            if key in ASSERTION_KEYWORDS or key in ANNOTATION_KEYWORDS:
                continue
            found.append((pointer, key))
        fmt = node.get("format")
        if isinstance(fmt, str) and fmt not in _FORMATS:
            found.append((pointer, "format=%s" % fmt))
    return sorted(set(found))


def _resolve(ref, root, pointer):
    if not isinstance(ref, str) or not ref.startswith("#"):
        raise SchemaError("%s: $ref %r points outside this document, which is not supported"
                          % (pointer, ref))
    target = root
    for raw in ref[1:].split("/"):
        if raw == "":
            continue
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(target, list):
            try:
                target = target[int(token)]
                continue
            except (ValueError, IndexError):
                raise SchemaError("%s: $ref %r does not resolve" % (pointer, ref))
        if not isinstance(target, dict) or token not in target:
            raise SchemaError("%s: $ref %r does not resolve" % (pointer, ref))
        target = target[token]
    return target


def _type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_matches(value, name):
    actual = _type_name(value)
    if name == "number":
        return actual in ("number", "integer")
    return actual == name


def _numeric(value):
    """The value as a number, or None. Booleans are not numbers, whatever Python thinks."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def validate(instance, schema=None, root=None, path="$", pointer="#"):
    """Validate ``instance``. Returns a list of {"path", "message"} dicts, empty when valid.

    ``path`` names the place in the INSTANCE, which is what an author needs to fix; ``pointer``
    names the place in the SCHEMA, which is what a template maintainer needs.
    """
    schema = load_schema() if schema is None else schema
    root = schema if root is None else root
    if schema is True:
        return []
    if schema is False:
        return [{"path": path, "message": "nothing is valid here (schema is false)"}]
    if not isinstance(schema, dict):
        raise SchemaError("%s: schema node is a %s, not an object or boolean"
                          % (pointer, type(schema).__name__))

    out = []

    def bad(message):
        out.append({"path": path, "message": message})

    if "$ref" in schema:
        target = _resolve(schema["$ref"], root, pointer)
        out.extend(validate(instance, target, root, path, schema["$ref"]))
        # 2020-12 lets $ref sit beside other keywords, and run-schema.json uses that, so the
        # sibling keywords below still apply. Falling through is deliberate.

    if "type" in schema:
        wanted = schema["type"]
        names = wanted if isinstance(wanted, list) else [wanted]
        if not any(_type_matches(instance, n) for n in names):
            bad("expected %s, found %s" % (" or ".join(names), _type_name(instance)))
            # Every remaining keyword is type-specific, so continuing would bury the one finding
            # that matters under a pile of consequences of it.
            return out

    if "enum" in schema and instance not in schema["enum"]:
        bad("%r is not one of %s" % (instance, ", ".join(repr(v) for v in schema["enum"])))
    if "const" in schema and instance != schema["const"]:
        bad("expected the constant %r, found %r" % (schema["const"], instance))

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            bad("string is %d character(s), the minimum is %d"
                % (len(instance), schema["minLength"]))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            bad("string is %d character(s), the maximum is %d"
                % (len(instance), schema["maxLength"]))
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            bad("%r does not match the pattern %s" % (instance, schema["pattern"]))
        fmt = schema.get("format")
        if isinstance(fmt, str):
            rx = _FORMATS.get(fmt)
            if rx is None:
                raise SchemaError("%s: format %r is not implemented" % (pointer, fmt))
            if not rx.match(instance):
                bad("%r is not a valid %s" % (instance, fmt))

    number = _numeric(instance)
    if number is not None:
        for key, ok, word in (("minimum", lambda a, b: a >= b, "at least"),
                              ("maximum", lambda a, b: a <= b, "at most"),
                              ("exclusiveMinimum", lambda a, b: a > b, "greater than"),
                              ("exclusiveMaximum", lambda a, b: a < b, "less than")):
            if key in schema and not ok(number, schema[key]):
                bad("%s must be %s %s" % (number, word, schema[key]))
        if "multipleOf" in schema and schema["multipleOf"]:
            quotient = number / schema["multipleOf"]
            if abs(quotient - round(quotient)) > 1e-9:
                bad("%s is not a multiple of %s" % (number, schema["multipleOf"]))

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            bad("array holds %d item(s), the minimum is %d" % (len(instance), schema["minItems"]))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            bad("array holds %d item(s), the maximum is %d" % (len(instance), schema["maxItems"]))
        if schema.get("uniqueItems"):
            seen = []
            for item in instance:
                if item in seen:
                    bad("array items must be unique; %r repeats" % (item,))
                    break
                seen.append(item)
        if "items" in schema:
            for i, item in enumerate(instance):
                out.extend(validate(item, schema["items"], root, "%s[%d]" % (path, i),
                                    pointer + "/items"))
        if "contains" in schema:
            # How the schema says "exactly one of these runs is the primary one". The count is the
            # assertion, so minContains and maxContains are read here rather than being annotations
            # on a keyword that only ever checked for one.
            hits = sum(1 for item in instance
                       if not validate(item, schema["contains"], root, path,
                                       pointer + "/contains"))
            low = schema.get("minContains", 1)
            high = schema.get("maxContains")
            if hits < low:
                bad("%d item(s) match the required shape, the minimum is %d" % (hits, low))
            if high is not None and hits > high:
                bad("%d item(s) match the required shape, the maximum is %d" % (hits, high))

    if isinstance(instance, dict):
        for name in schema.get("required") or []:
            if name not in instance:
                bad("required property %r is missing" % name)
        if "propertyNames" in schema:
            for key in sorted(instance):
                for item in validate(key, schema["propertyNames"], root,
                                     "%s (property name %r)" % (path, key),
                                     pointer + "/propertyNames"):
                    out.append(item)
        props = schema.get("properties") or {}
        patterns = schema.get("patternProperties") or {}
        extra = schema.get("additionalProperties")
        for key in sorted(instance):
            child = "%s.%s" % (path, key)
            matched = False
            if key in props:
                matched = True
                out.extend(validate(instance[key], props[key], root, child,
                                    "%s/properties/%s" % (pointer, key)))
            for rx, sub in patterns.items():
                if re.search(rx, key):
                    matched = True
                    out.extend(validate(instance[key], sub, root, child,
                                        "%s/patternProperties/%s" % (pointer, rx)))
            if matched or extra is None:
                continue
            if extra is False:
                bad("property %r is not allowed here" % key)
            elif extra is not True:
                out.extend(validate(instance[key], extra, root, child,
                                    pointer + "/additionalProperties"))

    for i, sub in enumerate(schema.get("allOf") or []):
        out.extend(validate(instance, sub, root, path, "%s/allOf/%d" % (pointer, i)))
    if "anyOf" in schema:
        branches = [validate(instance, sub, root, path, "%s/anyOf/%d" % (pointer, i))
                    for i, sub in enumerate(schema["anyOf"])]
        if branches and not any(not b for b in branches):
            bad("matches none of the %d permitted shapes; the closest reports: %s"
                % (len(branches), min(branches, key=len)[0]["message"]))
    if "oneOf" in schema:
        branches = [validate(instance, sub, root, path, "%s/oneOf/%d" % (pointer, i))
                    for i, sub in enumerate(schema["oneOf"])]
        passing = [i for i, b in enumerate(branches) if not b]
        if len(passing) != 1:
            if not passing:
                bad("matches none of the %d permitted shapes; the closest reports: %s"
                    % (len(branches), min(branches, key=len)[0]["message"]))
            else:
                bad("matches %d of the permitted shapes and must match exactly one"
                    % len(passing))
    if "not" in schema and not validate(instance, schema["not"], root, path, pointer + "/not"):
        bad("matches a shape that is forbidden here")

    if "if" in schema:
        # if/then/else is how the schema says "a value of kind X owes field Y". The `if` branch's
        # own findings are discarded on purpose: failing it is not an error, it selects `else`.
        if not validate(instance, schema["if"], root, path, pointer + "/if"):
            if "then" in schema:
                out.extend(validate(instance, schema["then"], root, path, pointer + "/then"))
        elif "else" in schema:
            out.extend(validate(instance, schema["else"], root, path, pointer + "/else"))

    return out


def summarise(schema):
    """A short human-readable map of the schema: what a bundle must have, and what may follow.

    Printing 88 KB of JSON is not reading it. This is the view that answers "what does a run
    bundle have to contain", which is the question someone typing `gpubench template schema` has.
    """
    lines = []
    lines.append("%s" % (schema.get("title") or "run schema"))
    if schema.get("$id"):
        lines.append("id      : %s" % schema["$id"])
    lines.append("dialect : %s" % (schema.get("$schema") or "(unstated)"))
    required = set(schema.get("required") or [])
    props = schema.get("properties") or {}
    lines.append("")
    lines.append("TOP-LEVEL PROPERTIES  (%d, of which %d required)" % (len(props), len(required)))
    for name in sorted(props):
        node = props[name] or {}
        kind = node.get("type") or ("$ref " + node["$ref"] if node.get("$ref") else "any")
        if isinstance(kind, list):
            kind = "|".join(kind)
        text = (node.get("description") or "").strip().replace("\n", " ")
        lines.append("  %-1s %-26s %-16s %s"
                     % ("*" if name in required else " ", name, kind, text[:90]))
    lines.append("")
    lines.append("  * = required")
    defs = schema.get("$defs") or {}
    if defs:
        lines.append("")
        lines.append("DEFINITIONS  (%d, referenced by $ref)" % len(defs))
        for name in sorted(defs):
            text = ((defs[name] or {}).get("description") or "").strip().replace("\n", " ")
            lines.append("  %-30s %s" % (name, text[:80]))
    gaps = unsupported_keywords(schema)
    lines.append("")
    if gaps:
        lines.append("NOT ENFORCED by this validator: %s"
                     % ", ".join(sorted({k for _p, k in gaps})))
    else:
        lines.append("Every keyword in this schema is enforced by "
                     "`gpubench template schema --validate`.")
    return "\n".join(lines)
