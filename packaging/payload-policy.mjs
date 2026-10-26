// Pure packaging rules: importing this module never creates a build directory.
export function shouldCopyPayload(tree, relativePath) {
  const components = String(relativePath).replaceAll('\\', '/').split('/');
  // dbt's directories called "tests" include executable production macros.
  // Verified dependency trees are not subject to source/test exclusions.
  const excluded = tree === 'runtime'
    ? ['__pycache__', '.pytest_cache']
    : ['node_modules', '__pycache__', '.pytest_cache', '.git', 'tests'];
  return !components.some(part => excluded.includes(part))
    && !/\.pyc$/i.test(relativePath)
    && !(tree !== 'runtime' && /\.log$/i.test(relativePath));
}

export function verifyRuntimeInventory(manifest, inventory, prefix = 'resources/app/') {
  if (!manifest || !Array.isArray(manifest.files) || !manifest.files.length)
    throw new Error('Runtime manifest must contain a non-empty files inventory.');
  const seen = new Set();
  let bytes = 0;
  for (const record of manifest.files) {
    const name = record?.path;
    if (typeof name !== 'string' || !name.startsWith('runtime/') || name.includes('\\')
        || name.includes(':') || name.split('/').some(part => !part || part === '.' || part === '..')
        || !Number.isSafeInteger(record.bytes) || record.bytes < 0
        || typeof record.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(record.sha256))
      throw new Error('Runtime manifest contains an invalid file record.');
    const normalized = name.toLowerCase();
    if (seen.has(normalized)) throw new Error('Runtime manifest contains a duplicate file path: ' + name);
    seen.add(normalized);
    const actual = inventory.get(prefix + name);
    if (!actual) throw new Error('Missing bundled runtime payload: ' + name);
    if (actual.bytes !== record.bytes) throw new Error('Bundled runtime size mismatch: ' + name);
    if (actual.sha256 !== record.sha256) throw new Error('Bundled runtime hash mismatch: ' + name);
    bytes += record.bytes;
  }
  for (const name of inventory.keys()) {
    if (name.startsWith(prefix + 'runtime/') && !seen.has(name.slice(prefix.length).toLowerCase()))
      throw new Error('Bundled runtime file is absent from its manifest: ' + name.slice(prefix.length));
  }
  return {files: manifest.files.length, bytes};
}
