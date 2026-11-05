// Pathological JavaScript: complex nested expressions and patterns

const MIN_LENGTH = 2;
const LOOKUP_SIZE = 20;

// Long single-line chained expression
const deepChain = (data) => data.flatMap(outer => outer.flatMap(inner => inner.filter(s => s && s.trim().length >= MIN_LENGTH).map(s => s.trim().toLowerCase().replace(/\s+/g, "_").replace(/[^a-z0-9_]/g, "")))).filter((v, i, a) => a.indexOf(v) === i).sort();

// Deeply nested ternary (valid but complex)
const classify = (x) =>
  x < 0
    ? x < -1000
      ? x < -10000 ? "astronomically negative" : "deeply negative"
      : x < -100 ? "moderately negative" : "slightly negative"
    : x === 0
    ? "zero"
    : x < 100
    ? x < 10 ? "tiny" : "small"
    : x < 10000
    ? x < 1000 ? "medium" : "large"
    : "huge";

// Destructuring with defaults and rename
function processConfig({
  host = "localhost",
  port: serverPort = 8080,
  tls: { enabled: tlsEnabled = false, cert: certPath = null } = {},
  timeout = 30000,
  retries: maxRetries = 3,
  pool: { min: poolMin = 2, max: poolMax = 10 } = {},
} = {}) {
  return { host, serverPort, tlsEnabled, certPath, timeout, maxRetries, poolMin, poolMax };
}

// Array and object spread with complex expressions
function mergeConfigs(...configs) {
  return configs.reduce(
    (acc, cfg) => ({
      ...acc,
      ...cfg,
      tags: [...(acc.tags ?? []), ...(cfg.tags ?? [])],
      metadata: { ...(acc.metadata ?? {}), ...(cfg.metadata ?? {}) },
    }),
    {}
  );
}

// Computed property names and method shorthand
const handlers = {
  [`on${"Click"}`]: (e) => console.log("click", e),
  [`on${"Focus"}`]: (e) => console.log("focus", e),
  [`on${"Blur"}`]: (e) => console.log("blur", e),
};

// Optional chaining and nullish coalescing chains
const safeGet = (obj, ...keys) =>
  keys.reduce((acc, key) => acc?.[key] ?? null, obj);

// IIFE with complex body
const processedLookup = (() => {
  const result = {};
  for (let i = 0; i < LOOKUP_SIZE; i++) {
    result[`key_${i}`] = {
      value: i,
      label: `Item ${i}`,
      even: i % 2 === 0,
      factors: Array.from({ length: i }, (_, j) => j + 1).filter((j) => i % j === 0),
    };
  }
  return Object.freeze(result);
})();

export { deepChain, classify, processConfig, mergeConfigs, handlers, safeGet, processedLookup };
