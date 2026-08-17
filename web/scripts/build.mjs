/**
 * "Build" for a dependency-free static site: assemble web/dist from the
 * source tree so the CI artifact is exactly what a static host serves.
 */
import { cpSync, mkdirSync, rmSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const webRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const dist = join(webRoot, 'dist');

rmSync(dist, { recursive: true, force: true });
mkdirSync(dist, { recursive: true });
cpSync(join(webRoot, 'index.html'), join(dist, 'index.html'));
cpSync(join(webRoot, 'assets'), join(dist, 'assets'), { recursive: true });
cpSync(join(webRoot, 'src'), join(dist, 'src'), { recursive: true });

if (!existsSync(join(dist, 'index.html'))) {
  console.error('build failed: dist/index.html missing');
  process.exit(1);
}
console.error('build ok: web/dist assembled');
