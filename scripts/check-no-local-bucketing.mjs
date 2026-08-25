#!/usr/bin/env node
/**
 * R36: bucket derivation is server-only.
 *
 * The platform decides which variant a person gets, by hashing (experienceId, identifier) and
 * taking the result modulo the bucket count. No SDK may do that arithmetic itself.
 *
 * The failure this prevents is silent and unreportable: two derivations that disagree serve the
 * same person a different variant depending on which channel they arrive through. Nothing in the
 * product surfaces it, because each side is internally consistent.
 *
 * This was true of all SDKs by accident before it was a rule. This guard makes it a fact.
 *
 * Zero dependencies, so it runs before install and on a machine that cannot build this SDK.
 *
 * An entry may be allowed — hashing has legitimate non-bucketing uses, idempotency keys and cache
 * keys among them — by listing it in the sidecar allowlist with a reason. An allowlist entry that
 * no longer matches anything is itself an error, so the file cannot silently rot.
 */

import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, relative, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = process.env.GUARD_ROOT ?? join(here, '..');
const roots = (process.env.GUARD_SRC ?? 'src').split(',').map((d) => d.trim()).filter(Boolean);
const allowPath = join(here, 'no-local-bucketing-allow.json');

const SOURCE = /\.(ts|tsx|js|mjs|cjs|py|php|kt|java|swift)$/;
const SKIP_DIR = /^(node_modules|\.git|dist|build|vendor|target|__pycache__|\.venv|Pods|DerivedData)$/;

/** Hashing primitives, and the bucket arithmetic itself. */
const PATTERNS = [
  [/\b(sha-?256|sha-?1|md5|murmur|fnv|crc32|xxhash)\b/i, 'a hashing primitive'],
  [/%\s*10000\b|\bmod\s+10000\b/i, 'modulo the platform bucket count'],
  [/\bBUCKETS?_PER_|\bTOTAL_BUCKETS\b/i, 'bucket arithmetic'],
  [/createHash|MessageDigest|hashlib|CryptoKit|\bDigest\b/, 'a hash construction'],
];

function walk(dir, out = []) {
  if (!existsSync(dir)) return out;
  for (const name of readdirSync(dir)) {
    if (SKIP_DIR.test(name)) continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (SOURCE.test(name)) out.push(p);
  }
  return out;
}

const allow = existsSync(allowPath) ? JSON.parse(readFileSync(allowPath, 'utf8')) : {};
const seen = new Set();
const hits = [];

for (const r of roots) {
  for (const file of walk(join(root, r))) {
    const rel = relative(root, file);
    readFileSync(file, 'utf8').split('\n').forEach((line, i) => {
      if (/^\s*(\/\/|#|\*|--)/.test(line)) return; // a comment explaining the rule is not a breach
      for (const [re, what] of PATTERNS) {
        if (!re.test(line)) continue;
        const key = `${rel}:${i + 1}`;
        if (allow[key] || allow[rel]) { seen.add(allow[key] ? key : rel); return; }
        hits.push(`${key}  ${what}\n      ${line.trim().slice(0, 100)}`);
        return;
      }
    });
  }
}

const problems = [];
if (hits.length) {
  problems.push(
    `bucket derivation must be server-only (R36) — ${hits.length} occurrence(s):\n    ` +
      hits.join('\n    ')
  );
}

// The reverse check. Without it the allowlist becomes a place to park anything, and a stale entry
// hides the fact that its justification no longer applies.
const stale = Object.keys(allow).filter((k) => !seen.has(k));
if (stale.length) {
  problems.push(`allowlist entries that no longer match anything: ${stale.join(', ')}`);
}
const unexplained = Object.entries(allow).filter(([, v]) => !String(v ?? '').trim());
if (unexplained.length) {
  problems.push(`allowlist entries with no reason: ${unexplained.map(([k]) => k).join(', ')}`);
}

if (problems.length) {
  console.error('no-local-bucketing FAILED');
  for (const p of problems) console.error(`  - ${p}`);
  process.exit(1);
}
console.log(
  `no-local-bucketing OK — scanned ${roots.join(', ')}, ${Object.keys(allow).length} documented allowance(s)`
);
