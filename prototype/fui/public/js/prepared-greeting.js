const DATABASE = 'moss-fui-prepared-greeting';
const STORE = 'audio';
const ENTRY = 'startup';
const OPEN_TIMEOUT_MS = 1200;

function openDatabase() {
  return new Promise((resolve, reject) => {
    if (!globalThis.indexedDB) throw new Error('IndexedDB is unavailable');
    const request = globalThis.indexedDB.open(DATABASE, 1);
    let abandoned = false;
    const timer = setTimeout(() => fail(new Error('IndexedDB did not open in time')), OPEN_TIMEOUT_MS);
    function fail(error) {
      abandoned = true;
      clearTimeout(timer);
      reject(error);
    }
    request.onblocked = () => fail(new Error('IndexedDB is blocked by another tab'));
    request.onupgradeneeded = () => {
      if (abandoned) request.transaction.abort();
      else request.result.createObjectStore(STORE);
    };
    request.onerror = () => fail(request.error);
    request.onsuccess = () => {
      clearTimeout(timer);
      const database = request.result;
      if (abandoned) database.close();
      else resolve(database);
    };
  });
}

async function access(mode, useStore) {
  const database = await openDatabase();
  try {
    return await new Promise((resolve, reject) => {
      const transaction = database.transaction(STORE, mode);
      const request = useStore(transaction.objectStore(STORE));
      transaction.oncomplete = () => resolve(request.result);
      transaction.onabort = () => reject(transaction.error || new Error('Greeting cache transaction aborted'));
    });
  } finally {
    database.close();
  }
}

function hasAudio(blobs) {
  return Array.isArray(blobs) && blobs.length > 0 &&
    blobs.every((blob) => blob instanceof Blob && blob.size > 0);
}

export function createGreetingAudioCache() {
  return {
    async read(text, roomAmount) {
      try {
        const record = await access('readonly', (store) => store.get(ENTRY));
        if (record && record.text === text && record.roomAmount === roomAmount && hasAudio(record.blobs)) {
          return record.blobs;
        }
      } catch {
        // A private or blocked database should behave like an empty cache.
      }
      return null;
    },

    async write(text, roomAmount, blobs) {
      if (typeof text !== 'string' || !text.trim() || !Number.isFinite(roomAmount) || !hasAudio(blobs)) {
        throw new TypeError('A greeting, room amount and nonempty audio blobs are required');
      }
      await access('readwrite', (store) => store.put({ text, roomAmount, blobs }, ENTRY));
    },
  };
}
