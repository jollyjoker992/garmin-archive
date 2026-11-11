// Validate FHIR resources with the same validator the Medplum server uses.
//   cd tools/fhir-validate && npm ci && node validate.mjs <file.json|file.ndjson|dir>...
// Exit code 1 if any resource has an error.
import { indexStructureDefinitionBundle, validateResource } from '@medplum/core';
import { readJson } from '@medplum/definitions';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

for (const f of ['fhir/r4/profiles-types.json', 'fhir/r4/profiles-resources.json', 'fhir/r4/profiles-medplum.json']) {
  indexStructureDefinitionBundle(readJson(f));
}

function* files(path) {
  if (statSync(path).isDirectory()) {
    for (const name of readdirSync(path).sort()) yield* files(join(path, name));
  } else if (/\.(nd)?json$/.test(path)) {
    yield path;
  }
}

let total = 0;
let bad = 0;
for (const arg of process.argv.slice(2)) {
  for (const path of files(arg)) {
    const text = readFileSync(path, 'utf8');
    const docs = path.endsWith('.ndjson') ? text.split('\n').filter(Boolean) : [text];
    docs.forEach((doc, i) => {
      if (!/"resourceType"\s*:/.test(doc)) return; // not a FHIR resource (e.g. manifest.json)
      total++;
      const where = docs.length > 1 ? `${path}:${i + 1}` : path;
      try {
        for (const issue of validateResource(JSON.parse(doc))) {
          console.log(`${issue.severity} ${where}: ${issue.details?.text} @ ${issue.expression}`);
        }
      } catch (e) {
        bad++;
        const issues = e.outcome?.issue ?? [{ details: { text: String(e) } }];
        for (const issue of issues) console.log(`error ${where}: ${issue.details?.text} @ ${issue.expression ?? ''}`);
      }
    });
  }
}
console.log(`${total} resources, ${bad} invalid`);
process.exit(bad ? 1 : 0);
