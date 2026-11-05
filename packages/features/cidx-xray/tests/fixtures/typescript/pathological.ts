// Pathological TypeScript: deeply nested types and complex expressions

// Deeply nested generic type
type Deep<T, N extends number, Acc extends unknown[] = []> =
  Acc["length"] extends N ? T : Deep<T[], N, [...Acc, unknown]>;

// Complex conditional type chain
type Stringify<T> =
  T extends string ? T :
  T extends number ? `${T}` :
  T extends boolean ? `${T}` :
  T extends null ? "null" :
  T extends undefined ? "undefined" :
  T extends object ? "[object]" :
  "unknown";

// Long single-line type utilities
type Keys<T> = T extends object ? keyof T : never;
type Values<T> = T extends object ? T[keyof T] : never;
type Entries<T> = T extends object ? { [K in keyof T]: [K, T[K]] }[keyof T] : never;

// Intersection of many types
type Combined = { a: string } & { b: number } & { c: boolean } & { d: Date } & { e: unknown[] };

// Complex function overloads
function process(x: string): string;
function process(x: number): number;
function process(x: boolean): boolean;
function process(x: string | number | boolean): string | number | boolean {
  if (typeof x === "string") return x.toUpperCase();
  if (typeof x === "number") return x * 2;
  return !x;
}

// Deeply nested object literal
const config = {
  server: {
    host: "localhost",
    port: 8080,
    tls: {
      enabled: false,
      cert: { path: "/etc/ssl/cert.pem", key: { path: "/etc/ssl/key.pem", passphrase: "" } },
    },
  },
  database: {
    primary: { host: "db1", port: 5432, pool: { min: 2, max: 10, idleTimeout: 30000 } },
    replica: { host: "db2", port: 5432, pool: { min: 1, max: 5, idleTimeout: 60000 } },
  },
} as const;

// Many parameters
function buildUrl(
  scheme: string, host: string, port: number, path: string,
  query: Record<string, string>, fragment: string, trailingSlash: boolean
): string {
  const q = Object.entries(query).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  const base = `${scheme}://${host}:${port}/${path}${trailingSlash ? "/" : ""}`;
  return `${base}${q ? "?" + q : ""}${fragment ? "#" + fragment : ""}`;
}

// Chained promise with many .then calls (long line)
const result = Promise.resolve("hello").then(s => s.toUpperCase()).then(s => s.split("").reverse().join("")).then(s => s + "!").then(s => ({ value: s, length: s.length })).catch((e: unknown) => ({ value: "", length: 0, error: String(e) }));

export { process, buildUrl, config, result };
export type { Deep, Stringify, Combined };
