// Advanced TypeScript: conditional types, template literal types, mapped types

// Conditional types
type IsArray<T> = T extends unknown[] ? true : false;
type Flatten<T> = T extends Array<infer U> ? U : T;
type NonNullable<T> = T extends null | undefined ? never : T;

// Template literal types
type EventName<T extends string> = `on${Capitalize<T>}`;
type Getter<T extends string> = `get${Capitalize<T>}`;
type Setter<T extends string> = `set${Capitalize<T>}`;

type UserEvents = EventName<"click" | "focus" | "blur">;

// Mapped types
type Readonly<T> = { readonly [K in keyof T]: T[K] };
type Partial<T> = { [K in keyof T]?: T[K] };
type Required<T> = { [K in keyof T]-?: T[K] };
type Nullable<T> = { [K in keyof T]: T[K] | null };

// Recursive types
type DeepReadonly<T> = {
  readonly [K in keyof T]: T[K] extends object ? DeepReadonly<T[K]> : T[K];
};

type DeepPartial<T> = {
  [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K];
};

// Extract and Exclude
type StringOrNumber = string | number | boolean;
type OnlyStrings = Extract<StringOrNumber, string>;
type NoStrings = Exclude<StringOrNumber, string>;

// Infer in conditional types
type ReturnType<T> = T extends (...args: unknown[]) => infer R ? R : never;
type Parameters<T> = T extends (...args: infer P) => unknown ? P : never;
type PromiseValue<T> = T extends Promise<infer V> ? V : T;

// Discriminated union with exhaustive check
type Shape =
  | { kind: "circle"; radius: number }
  | { kind: "rectangle"; width: number; height: number }
  | { kind: "triangle"; base: number; height: number };

function area(shape: Shape): number {
  switch (shape.kind) {
    case "circle":
      return Math.PI * shape.radius ** 2;
    case "rectangle":
      return shape.width * shape.height;
    case "triangle":
      return 0.5 * shape.base * shape.height;
    default: {
      const _exhaustive: never = shape;
      throw new Error(`Unknown shape: ${JSON.stringify(_exhaustive)}`);
    }
  }
}

// Builder pattern with method chaining and type safety
class QueryBuilder<T extends Record<string, unknown>> {
  private conditions: string[] = [];
  private orderByClause?: string;
  private limitValue?: number;
  private offsetValue = 0;

  where(condition: string): this {
    this.conditions.push(condition);
    return this;
  }

  orderBy(field: keyof T & string, direction: "asc" | "desc" = "asc"): this {
    this.orderByClause = `${field} ${direction.toUpperCase()}`;
    return this;
  }

  limit(n: number): this {
    this.limitValue = n;
    return this;
  }

  offset(n: number): this {
    this.offsetValue = n;
    return this;
  }

  build(): string {
    let sql = "SELECT *";
    if (this.conditions.length > 0) {
      sql += ` WHERE ${this.conditions.join(" AND ")}`;
    }
    if (this.orderByClause !== undefined) {
      sql += ` ORDER BY ${this.orderByClause}`;
    }
    if (this.limitValue !== undefined) {
      sql += ` LIMIT ${this.limitValue} OFFSET ${this.offsetValue}`;
    }
    return sql;
  }
}

// Proxy-based observable
function observable<T extends object>(target: T, onChange: (key: keyof T, value: unknown) => void): T {
  return new Proxy(target, {
    set(obj, prop, value) {
      (obj as Record<string | symbol, unknown>)[prop] = value;
      onChange(prop as keyof T, value);
      return true;
    },
  });
}

// Symbol usage
const SERIALIZE = Symbol("serialize");
const DESERIALIZE = Symbol("deserialize");

interface Serializable {
  [SERIALIZE](): string;
  [DESERIALIZE](data: string): void;
}

export { area, QueryBuilder, observable, SERIALIZE, DESERIALIZE };
export type { Shape, DeepReadonly, DeepPartial, PromiseValue };
