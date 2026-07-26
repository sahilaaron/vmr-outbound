"use strict";
/**
 * A minimal JSON-Schema (draft 2020-12) evaluator covering exactly the subset
 * the committed contract schemas use, plus a keyword collector used to prove
 * the evaluator is not silently ignoring a construct.
 *
 * Test-only: it exists so the dependency-free extension validators can be
 * checked against the committed schema FILES, which are the single source of
 * truth for the wire shape.
 */

function typeOf(v) {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  if (Number.isInteger(v)) return "integer";
  if (typeof v === "number") return "number";
  return typeof v;
}

function typeMatches(declared, v) {
  const actual = typeOf(v);
  const list = Array.isArray(declared) ? declared : [declared];
  return list.some((t) => t === actual || (t === "number" && actual === "integer"));
}

function evaluate(schema, value, root) {
  root = root || schema;
  if (schema.$ref) {
    const m = /^#\/\$defs\/(.+)$/.exec(schema.$ref);
    if (!m) throw new Error(`unsupported $ref ${schema.$ref}`);
    return evaluate(root.$defs[m[1]], value, root);
  }
  if (schema.oneOf) {
    return schema.oneOf.filter((s) => evaluate(s, value, root)).length === 1;
  }
  if ("const" in schema && value !== schema.const) return false;
  if (schema.enum && !schema.enum.includes(value)) return false;
  if (schema.type && !typeMatches(schema.type, value)) return false;

  if (typeOf(value) === "string") {
    if (schema.minLength != null && value.length < schema.minLength) return false;
    if (schema.maxLength != null && value.length > schema.maxLength) return false;
    if (schema.pattern != null && !new RegExp(schema.pattern, "u").test(value)) return false;
  }
  if (typeOf(value) === "integer" || typeOf(value) === "number") {
    if (schema.minimum != null && value < schema.minimum) return false;
    if (schema.maximum != null && value > schema.maximum) return false;
  }
  if (Array.isArray(value)) {
    if (schema.maxItems != null && value.length > schema.maxItems) return false;
    if (schema.items && !value.every((item) => evaluate(schema.items, item, root))) return false;
  }
  if (typeOf(value) === "object") {
    for (const key of schema.required || []) {
      if (!(key in value)) return false;
    }
    const props = schema.properties || {};
    for (const [key, sub] of Object.entries(props)) {
      if (key in value && !evaluate(sub, value[key], root)) return false;
    }
    if (schema.additionalProperties === false) {
      for (const key of Object.keys(value)) {
        if (!(key in props)) return false;
      }
    }
  }
  return true;
}

const SUPPORTED = new Set([
  "$schema", "$id", "$defs", "$ref", "title", "description", "type", "const", "enum",
  "required", "properties", "additionalProperties", "items", "maxItems",
  "minLength", "maxLength", "pattern", "minimum", "maximum", "format", "oneOf",
]);
function collectKeywords(schema, found) {
  if (Array.isArray(schema)) return schema.forEach((s) => collectKeywords(s, found));
  if (schema && typeof schema === "object") {
    for (const [k, v] of Object.entries(schema)) {
      if (["properties", "$defs"].includes(k)) {
        Object.values(v).forEach((s) => collectKeywords(s, found));
      } else if (["items", "additionalProperties"].includes(k) && typeof v === "object") {
        collectKeywords(v, found);
      } else if (k === "oneOf") {
        collectKeywords(v, found);
      }
      found.add(k);
    }
  }
}


module.exports = { evaluate, collectKeywords, SUPPORTED, typeOf };
