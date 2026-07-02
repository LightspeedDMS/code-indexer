// Realistic JavaScript: async/await, classes, modules, modern patterns

const DEFAULT_PAGE_SIZE = 20;
const MAX_PAGE_SIZE = 100;
const CACHE_TTL_MS = 5 * 60 * 1000;

class UserService {
  #cache = new Map();
  #baseUrl;

  constructor(baseUrl) {
    this.#baseUrl = baseUrl;
  }

  async getUser(id) {
    const cached = this.#cache.get(id);
    if (cached && Date.now() - cached.ts < CACHE_TTL_MS) {
      return cached.user;
    }
    const user = await this.#fetch(`/users/${id}`);
    this.#cache.set(id, { user, ts: Date.now() });
    return user;
  }

  async listUsers({ page = 0, size = DEFAULT_PAGE_SIZE, role } = {}) {
    const effectiveSize = Math.min(Math.max(1, size), MAX_PAGE_SIZE);
    const params = new URLSearchParams({ page, size: effectiveSize });
    if (role !== undefined) params.set("role", role);
    return this.#fetch(`/users?${params}`);
  }

  async createUser({ name, email, role = "viewer" }) {
    if (!name?.trim()) throw new Error("name is required");
    if (!email?.includes("@")) throw new Error("invalid email");
    const user = await this.#fetch("/users", {
      method: "POST",
      body: JSON.stringify({ name: name.trim(), email: email.toLowerCase().trim(), role }),
    });
    this.#cache.set(user.id, { user, ts: Date.now() });
    return user;
  }

  async updateUser(id, updates) {
    const user = await this.#fetch(`/users/${id}`, {
      method: "PUT",
      body: JSON.stringify(updates),
    });
    this.#cache.set(id, { user, ts: Date.now() });
    return user;
  }

  async deleteUser(id) {
    await this.#fetch(`/users/${id}`, { method: "DELETE" });
    this.#cache.delete(id);
  }

  async #fetch(path, options = {}) {
    const response = await fetch(`${this.#baseUrl}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw Object.assign(new Error(body.message ?? response.statusText), { status: response.status });
    }
    return response.json();
  }

  invalidateCache(id) {
    if (id !== undefined) {
      this.#cache.delete(id);
    } else {
      this.#cache.clear();
    }
  }
}

// Functional utilities
function debounce(fn, waitMs) {
  let timer;
  return function debounced(...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), waitMs);
  };
}

function throttle(fn, intervalMs) {
  let lastCall = 0;
  return function throttled(...args) {
    const now = Date.now();
    if (now - lastCall >= intervalMs) {
      lastCall = now;
      return fn.apply(this, args);
    }
  };
}

function memoize(fn) {
  const cache = new Map();
  return function memoized(...args) {
    const key = JSON.stringify(args);
    if (cache.has(key)) return cache.get(key);
    const result = fn.apply(this, args);
    cache.set(key, result);
    return result;
  };
}

// Pipeline operator simulation
const pipe = (...fns) => (x) => fns.reduce((acc, fn) => fn(acc), x);

const processName = pipe(
  (s) => s.trim(),
  (s) => s.toLowerCase(),
  (s) => s.replace(/\s+/g, "_"),
  (s) => s.replace(/[^a-z0-9_]/g, "")
);

// Promise utilities
async function retry(fn, { maxAttempts = 3, delayMs = 100 } = {}) {
  let lastError;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (e) {
      lastError = e;
      if (attempt < maxAttempts - 1) {
        await new Promise((resolve) => setTimeout(resolve, delayMs * 2 ** attempt));
      }
    }
  }
  throw lastError;
}

async function allSettledMap(map) {
  const entries = Object.entries(map);
  const results = await Promise.allSettled(entries.map(([, fn]) => fn()));
  return Object.fromEntries(
    entries.map(([key], i) => [
      key,
      results[i].status === "fulfilled" ? results[i].value : results[i].reason,
    ])
  );
}

export { UserService, debounce, throttle, memoize, pipe, processName, retry, allSettledMap };
