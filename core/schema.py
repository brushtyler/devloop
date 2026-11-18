"""A small JSON Schema (draft 2020-12) validator for the schemas ``core`` reads as data.

It knows only the keywords listed in ``KEYWORDS`` and refuses a schema with any other one
instead of ignoring it: a constraint it cannot check is never treated as satisfied. Types,
equality and bounds follow the ``jsonschema`` package the engine uses: a boolean is neither an
integer nor a number, ``1.0`` is an integer, and ``true`` is not equal to ``1``.
"""
import re

# Keywords that only annotate a schema.
ANNOTATIONS = frozenset({'$schema', '$id', '$comment', '$defs', 'title', 'description', 'default',
                         'examples'})
KEYWORDS = frozenset({
    'type', 'const', 'enum', '$ref', 'oneOf', 'anyOf', 'allOf', 'not',
    'required', 'properties', 'additionalProperties', 'propertyNames', 'minProperties',
    'maxProperties', 'items', 'minItems', 'maxItems', 'uniqueItems',
    'minLength', 'maxLength', 'pattern', 'minimum', 'maximum', 'exclusiveMinimum',
    'exclusiveMaximum'})


class SchemaError(ValueError):
    """The document does not satisfy the schema, or the schema is outside what core checks."""


def validate(schema, document):
    """Raise ``SchemaError`` unless ``document`` satisfies ``schema``; return the document."""
    found = errors(schema, document)
    if found:
        raise SchemaError(found[0])
    return document


def errors(schema, document):
    """Every violation of ``schema`` by ``document``, as ``path: message`` strings."""
    out = []
    _check(schema, document, schema, '$', out)
    return out


def _is_type(value, name):
    if name == 'object':
        return isinstance(value, dict)
    if name == 'array':
        return isinstance(value, list)
    if name == 'string':
        return isinstance(value, str)
    if name == 'boolean':
        return isinstance(value, bool)
    if name == 'null':
        return value is None
    if name not in ('integer', 'number'):
        raise SchemaError('Unsupported schema type ' + repr(name))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return name == 'number' or isinstance(value, int) or value.is_integer()


def _equal(one, two):
    if isinstance(one, str) or isinstance(two, str):
        return one == two
    if isinstance(one, list) and isinstance(two, list):
        return len(one) == len(two) and all(_equal(a, b) for a, b in zip(one, two))
    if isinstance(one, dict) and isinstance(two, dict):
        return one.keys() == two.keys() and all(_equal(one[key], two[key]) for key in one)
    if isinstance(one, bool) or isinstance(two, bool):
        return type(one) is type(two) and one == two
    return one == two


def _resolve(root, ref):
    if not isinstance(ref, str) or not ref.startswith('#'):
        raise SchemaError('Only local schema references are supported: ' + repr(ref))
    node = root
    for part in ref[1:].split('/')[1:]:
        part = part.replace('~1', '/').replace('~0', '~')
        if not isinstance(node, dict) or part not in node:
            raise SchemaError('Unresolvable schema reference ' + repr(ref))
        node = node[part]
    return node


def _valid(schema, value, root):
    out = []
    _check(schema, value, root, '$', out)
    return not out


def _check(schema, value, root, path, out):
    if schema is True:
        return
    if schema is False:
        out.append(f'{path}: no value is allowed')
        return
    if not isinstance(schema, dict):
        raise SchemaError('Malformed schema at ' + path)
    unknown = set(schema) - KEYWORDS - ANNOTATIONS
    if unknown:
        raise SchemaError('Unsupported schema keyword: ' + ', '.join(sorted(unknown)))
    if '$ref' in schema:
        _check(_resolve(root, schema['$ref']), value, root, path, out)
    if 'type' in schema:
        names = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        if not any(_is_type(value, name) for name in names):
            out.append(f'{path}: is not of type {schema["type"]!r}')
            return
    if 'const' in schema and not _equal(value, schema['const']):
        out.append(f'{path}: must be {schema["const"]!r}')
    if 'enum' in schema and not any(_equal(value, option) for option in schema['enum']):
        out.append(f'{path}: is not one of {schema["enum"]!r}')
    for keyword in ('allOf', 'anyOf', 'oneOf'):
        if keyword in schema:
            matched = sum(_valid(sub, value, root) for sub in schema[keyword])
            if (keyword == 'allOf' and matched != len(schema[keyword]) or keyword == 'anyOf' and not matched
                    or keyword == 'oneOf' and matched != 1):
                out.append(f'{path}: does not satisfy {keyword}')
    if 'not' in schema and _valid(schema['not'], value, root):
        out.append(f'{path}: satisfies a schema it must not')
    if isinstance(value, dict):
        _check_object(schema, value, root, path, out)
    elif isinstance(value, list):
        _check_array(schema, value, root, path, out)
    elif isinstance(value, str):
        if 'minLength' in schema and len(value) < schema['minLength']:
            out.append(f'{path}: is too short')
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            out.append(f'{path}: is too long')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            out.append(f'{path}: does not match {schema["pattern"]!r}')
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        for keyword, fails in (('minimum', lambda v, m: v < m), ('maximum', lambda v, m: v > m),
                               ('exclusiveMinimum', lambda v, m: v <= m),
                               ('exclusiveMaximum', lambda v, m: v >= m)):
            if keyword in schema and fails(value, schema[keyword]):
                out.append(f'{path}: violates {keyword} {schema[keyword]!r}')


def _check_object(schema, value, root, path, out):
    for key in schema.get('required', ()):
        if key not in value:
            out.append(f'{path}: {key!r} is required')
    if 'minProperties' in schema and len(value) < schema['minProperties']:
        out.append(f'{path}: has too few properties')
    if 'maxProperties' in schema and len(value) > schema['maxProperties']:
        out.append(f'{path}: has too many properties')
    properties = schema.get('properties', {})
    for key, item in value.items():
        if 'propertyNames' in schema:
            _check(schema['propertyNames'], key, root, f'{path}[{key!r}] name', out)
        if key in properties:
            _check(properties[key], item, root, f'{path}.{key}', out)
        elif 'additionalProperties' in schema:
            _check(schema['additionalProperties'], item, root, f'{path}.{key}', out)


def _check_array(schema, value, root, path, out):
    if 'minItems' in schema and len(value) < schema['minItems']:
        out.append(f'{path}: has too few items')
    if 'maxItems' in schema and len(value) > schema['maxItems']:
        out.append(f'{path}: has too many items')
    if schema.get('uniqueItems') and any(_equal(value[i], value[j]) for i in range(len(value))
                                         for j in range(i + 1, len(value))):
        out.append(f'{path}: has non-unique items')
    if 'items' in schema:
        for index, item in enumerate(value):
            _check(schema['items'], item, root, f'{path}[{index}]', out)
