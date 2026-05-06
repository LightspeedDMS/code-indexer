// Advanced JavaScript: generators, proxies, WeakMap, Symbol, iterators

// Symbol-based private state
const _state = Symbol("state");
const _transitions = Symbol("transitions");

class StateMachine {
  constructor(initialState, transitions) {
    this[_state] = initialState;
    this[_transitions] = transitions;
  }

  get state() { return this[_state]; }

  transition(event) {
    const key = `${this[_state]}:${event}`;
    const next = this[_transitions][key];
    if (next === undefined) {
      throw new Error(`No transition from ${this[_state]} on ${event}`);
    }
    this[_state] = next;
    return this;
  }
}

// Generator-based range and infinite sequences
function* range(start, end, step = 1) {
  for (let i = start; i < end; i += step) {
    yield i;
  }
}

function* fibonacci() {
  let [a, b] = [0, 1];
  while (true) {
    yield a;
    [a, b] = [b, a + b];
  }
}

function take(gen, n) {
  const result = [];
  for (const value of gen) {
    result.push(value);
    if (result.length >= n) break;
  }
  return result;
}

// Proxy-based observable store
function createStore(initial) {
  const listeners = new Set();
  let state = { ...initial };

  const proxy = new Proxy(state, {
    set(target, prop, value) {
      target[prop] = value;
      listeners.forEach((fn) => fn({ prop, value, state: { ...target } }));
      return true;
    },
    get(target, prop) {
      if (prop === "subscribe") {
        return (fn) => {
          listeners.add(fn);
          return () => listeners.delete(fn);
        };
      }
      return target[prop];
    },
  });

  return proxy;
}

// WeakMap for private data
const privateData = new WeakMap();

class SecureCounter {
  constructor(initial = 0) {
    privateData.set(this, { count: initial, history: [] });
  }

  increment(amount = 1) {
    const data = privateData.get(this);
    data.history.push(data.count);
    data.count += amount;
    return this;
  }

  get value() { return privateData.get(this).count; }
  get history() { return [...privateData.get(this).history]; }
}

// Async generator
async function* streamPages(fetchPage, maxPages = 10) {
  let page = 0;
  while (page < maxPages) {
    const data = await fetchPage(page);
    if (!data || data.length === 0) break;
    yield* data;
    page++;
  }
}

// Tagged template literal
function sql(strings, ...values) {
  const params = [];
  const query = strings.reduce((acc, str, i) => {
    if (i < values.length) {
      params.push(values[i]);
      return acc + str + `$${params.length}`;
    }
    return acc + str;
  }, "");
  return { query, params };
}

// Iterable class
class Range {
  constructor(start, end) {
    this.start = start;
    this.end = end;
  }

  [Symbol.iterator]() {
    let current = this.start;
    const end = this.end;
    return {
      next() {
        if (current <= end) {
          return { value: current++, done: false };
        }
        return { value: undefined, done: true };
      },
    };
  }
}

export { StateMachine, range, fibonacci, take, createStore, SecureCounter, streamPages, sql, Range };
