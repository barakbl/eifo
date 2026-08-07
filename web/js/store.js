/* A minimal observable store.
 *
 * State flows one way: views read it, call setters, and re-render from the
 * change notification. There is no two-way binding and no framework — the app
 * has few enough moving parts that this is the whole state layer.
 */

export function createStore(initial = {}) {
  let state = { ...initial };
  const listeners = new Set();

  function get() {
    return state;
  }

  /**
   * Merge a patch into state and notify listeners.
   *
   * A patch that changes nothing notifies nobody, which keeps a re-render from
   * being triggered by, say, typing the same search term twice.
   */
  function set(patch) {
    const next = { ...state, ...patch };
    if (isShallowEqual(state, next)) return state;

    state = next;
    for (const listener of [...listeners]) listener(state);
    return state;
  }

  function subscribe(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  return { get, set, subscribe };
}

/** Shallow equality, with arrays compared by their contents. */
export function isShallowEqual(left, right) {
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
  for (const key of keys) {
    const a = left[key];
    const b = right[key];
    if (Array.isArray(a) && Array.isArray(b)) {
      if (a.length !== b.length || a.some((value, index) => value !== b[index])) return false;
    } else if (a !== b) {
      return false;
    }
  }
  return true;
}

/**
 * Delay a function until calls stop arriving.
 *
 * Used for search-as-you-type: a request per keystroke would be both slow and
 * rude to the server.
 */
export function debounce(fn, wait) {
  let timer = null;
  const debounced = (...args) => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, wait);
  };
  debounced.cancel = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };
  return debounced;
}
