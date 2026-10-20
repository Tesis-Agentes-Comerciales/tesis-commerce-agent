// Test-only checker for the JSON Schema keywords governance-monitor-3.schema.json uses; no runtime dependency.
// Any other keyword throws, so the schema cannot rely on semantics this checker would silently skip.
const ANNOTATIONS = ["$schema", "$id", "$defs", "title", "description"]
const KEYWORDS = new Set([...ANNOTATIONS, "$ref", "type", "properties", "required", "additionalProperties", "propertyNames", "items", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "pattern", "minimum", "maximum", "enum", "const", "anyOf"])
const REF = /^#\/\$defs\/([A-Za-z0-9_]+)$/

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value)
const hasType = (value, type) => ({
  null: value === null, boolean: typeof value === "boolean", string: typeof value === "string",
  number: typeof value === "number", integer: Number.isInteger(value), array: Array.isArray(value), object: isObject(value),
})[type] ?? (() => { throw Error(`Unsupported type ${type}`) })()
function equal(a, b) {
  if (Array.isArray(a) || Array.isArray(b)) return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((x, i) => equal(x, b[i]))
  if (isObject(a) || isObject(b)) return isObject(a) && isObject(b) && Object.keys(a).length === Object.keys(b).length && Object.keys(a).every((k) => Object.hasOwn(b, k) && equal(a[k], b[k]))
  return a === b
}

/** Returns validate(value) -> list of error paths; an empty list means the value conforms. */
export function schemaValidator(root) {
  const definitions = root.$defs ?? {}
  const resolve = (ref) => {
    const name = REF.exec(ref)?.[1]
    if (!name || !Object.hasOwn(definitions, name)) throw Error(`Unsupported $ref ${ref}`)
    return definitions[name]
  }
  // Walk every subschema up front, not only those a given value happens to reach.
  const walk = (schema, at) => {
    if (typeof schema === "boolean") return
    if (!isObject(schema)) throw Error(`Invalid schema at ${at}`)
    for (const key of Object.keys(schema)) if (!KEYWORDS.has(key)) throw Error(`Unsupported keyword ${key} at ${at}`)
    if (schema.$ref !== undefined) resolve(schema.$ref)
    for (const [name, sub] of Object.entries(schema.$defs ?? {})) walk(sub, `${at}/$defs/${name}`)
    for (const [name, sub] of Object.entries(schema.properties ?? {})) walk(sub, `${at}/properties/${name}`)
    for (const key of ["additionalProperties", "propertyNames", "items"]) if (schema[key] !== undefined) walk(schema[key], `${at}/${key}`)
    for (const [i, sub] of (schema.anyOf ?? []).entries()) walk(sub, `${at}/anyOf/${i}`)
  }
  walk(root, "#")

  const errors = (schema, value, path, out) => {
    if (schema === true) return out
    if (schema === false) { out.push(`${path}: not allowed`); return out }
    if (schema.$ref !== undefined) errors(resolve(schema.$ref), value, path, out)
    if (schema.type !== undefined && ![schema.type].flat().some((type) => hasType(value, type))) out.push(`${path}: type`)
    if (schema.enum !== undefined && !schema.enum.some((x) => equal(x, value))) out.push(`${path}: enum`)
    if (Object.hasOwn(schema, "const") && !equal(schema.const, value)) out.push(`${path}: const`)
    if (typeof value === "string") {
      const length = [...value].length
      if (schema.minLength !== undefined && length < schema.minLength) out.push(`${path}: minLength`)
      if (schema.maxLength !== undefined && length > schema.maxLength) out.push(`${path}: maxLength`)
      if (schema.pattern !== undefined && !new RegExp(schema.pattern, "u").test(value)) out.push(`${path}: pattern`)
    }
    if (typeof value === "number") {
      if (schema.minimum !== undefined && value < schema.minimum) out.push(`${path}: minimum`)
      if (schema.maximum !== undefined && value > schema.maximum) out.push(`${path}: maximum`)
    }
    if (Array.isArray(value)) {
      if (schema.minItems !== undefined && value.length < schema.minItems) out.push(`${path}: minItems`)
      if (schema.maxItems !== undefined && value.length > schema.maxItems) out.push(`${path}: maxItems`)
      if (schema.uniqueItems === true && value.some((x, i) => value.slice(0, i).some((y) => equal(x, y)))) out.push(`${path}: uniqueItems`)
      if (schema.items !== undefined) value.forEach((x, i) => errors(schema.items, x, `${path}/${i}`, out))
    }
    if (isObject(value)) {
      for (const key of schema.required ?? []) if (!Object.hasOwn(value, key)) out.push(`${path}/${key}: required`)
      for (const [key, x] of Object.entries(value)) {
        if (schema.propertyNames !== undefined) errors(schema.propertyNames, key, `${path}/${key} (name)`, out)
        if (schema.properties && Object.hasOwn(schema.properties, key)) errors(schema.properties[key], x, `${path}/${key}`, out)
        else if (schema.additionalProperties !== undefined) errors(schema.additionalProperties, x, `${path}/${key}`, out)
      }
    }
    if (schema.anyOf !== undefined && !schema.anyOf.some((sub) => errors(sub, value, path, []).length === 0)) out.push(`${path}: anyOf`)
    return out
  }
  return (value) => errors(root, value, "", [])
}
