// Realistic TypeScript: React-style component with hooks and generic utilities

interface User {
  id: number;
  name: string;
  email: string;
  role: "admin" | "editor" | "viewer";
  createdAt: Date;
  enabled: boolean;
}

interface PageResult<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
}

interface ApiError {
  code: number;
  message: string;
  details?: Record<string, string[]>;
}

type Result<T> =
  | { ok: true; value: T }
  | { ok: false; error: ApiError };

function ok<T>(value: T): Result<T> {
  return { ok: true, value };
}

function err(error: ApiError): Result<never> {
  return { ok: false, error };
}

// Generic fetch utility
async function apiFetch<T>(
  url: string,
  options?: RequestInit
): Promise<Result<T>> {
  try {
    const response = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      return err({ code: response.status, message: body.message ?? response.statusText });
    }

    const data = (await response.json()) as T;
    return ok(data);
  } catch (e) {
    return err({ code: 0, message: e instanceof Error ? e.message : "Network error" });
  }
}

// User service class
class UserService {
  private readonly baseUrl: string;
  private cache = new Map<number, User>();

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
  }

  async getUser(id: number): Promise<Result<User>> {
    const cached = this.cache.get(id);
    if (cached !== undefined) {
      return ok(cached);
    }
    const result = await apiFetch<User>(`${this.baseUrl}/users/${id}`);
    if (result.ok) {
      this.cache.set(id, result.value);
    }
    return result;
  }

  async listUsers(
    page = 0,
    size = 20,
    role?: User["role"]
  ): Promise<Result<PageResult<User>>> {
    const params = new URLSearchParams({ page: String(page), size: String(size) });
    if (role !== undefined) {
      params.set("role", role);
    }
    return apiFetch<PageResult<User>>(`${this.baseUrl}/users?${params}`);
  }

  async createUser(
    name: string,
    email: string,
    role: User["role"] = "viewer"
  ): Promise<Result<User>> {
    const result = await apiFetch<User>(`${this.baseUrl}/users`, {
      method: "POST",
      body: JSON.stringify({ name, email, role }),
    });
    if (result.ok) {
      this.cache.set(result.value.id, result.value);
    }
    return result;
  }

  async updateUser(
    id: number,
    updates: Partial<Pick<User, "name" | "email" | "role" | "enabled">>
  ): Promise<Result<User>> {
    const result = await apiFetch<User>(`${this.baseUrl}/users/${id}`, {
      method: "PUT",
      body: JSON.stringify(updates),
    });
    if (result.ok) {
      this.cache.set(id, result.value);
    }
    return result;
  }

  invalidateCache(id?: number): void {
    if (id !== undefined) {
      this.cache.delete(id);
    } else {
      this.cache.clear();
    }
  }
}

// Generic debounce utility
function debounce<T extends (...args: unknown[]) => unknown>(
  fn: T,
  waitMs: number
): (...args: Parameters<T>) => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  return (...args: Parameters<T>): void => {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
    timer = setTimeout(() => {
      fn(...args);
    }, waitMs);
  };
}

// Generic memoize utility
function memoize<T extends (...args: unknown[]) => unknown>(fn: T): T {
  const cache = new Map<string, unknown>();
  return ((...args: unknown[]): unknown => {
    const key = JSON.stringify(args);
    if (cache.has(key)) {
      return cache.get(key);
    }
    const result = fn(...args);
    cache.set(key, result);
    return result;
  }) as T;
}

// Event emitter
type EventMap = Record<string, unknown>;
type EventKey<T extends EventMap> = string & keyof T;
type EventHandler<T> = (payload: T) => void;

class EventEmitter<T extends EventMap> {
  private handlers = new Map<string, Set<EventHandler<unknown>>>();

  on<K extends EventKey<T>>(event: K, handler: EventHandler<T[K]>): () => void {
    if (!this.handlers.has(event)) {
      this.handlers.set(event, new Set());
    }
    const set = this.handlers.get(event)!;
    set.add(handler as EventHandler<unknown>);
    return () => set.delete(handler as EventHandler<unknown>);
  }

  emit<K extends EventKey<T>>(event: K, payload: T[K]): void {
    this.handlers.get(event)?.forEach((h) => h(payload));
  }

  off<K extends EventKey<T>>(event: K, handler: EventHandler<T[K]>): void {
    this.handlers.get(event)?.delete(handler as EventHandler<unknown>);
  }
}

// Usage example
const service = new UserService("https://api.example.com");
const emitter = new EventEmitter<{ userCreated: User; userDeleted: number }>();

const unsubscribe = emitter.on("userCreated", (user) => {
  console.log("New user:", user.name);
});

const debouncedSearch = debounce(async (query: string) => {
  const result = await service.listUsers();
  if (result.ok) {
    console.log("Users:", result.value.items.length);
  }
}, 300);

export { UserService, EventEmitter, debounce, memoize, apiFetch, ok, err };
export type { User, PageResult, ApiError, Result };
