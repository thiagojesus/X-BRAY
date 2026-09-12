import { spawnSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repositoryRoot = resolve(frontendRoot, '..')
const baseFlag = process.argv.indexOf('--base')
const base = baseFlag >= 0 ? process.argv[baseFlag + 1] : process.env.BASE_SHA

if (!base) {
  throw new Error('lint:changed requires --base <sha> or BASE_SHA')
}

const changed = spawnSync('git', ['diff', '--name-only', '--diff-filter=ACMR', base, '--', 'frontend'], {
  cwd: repositoryRoot,
  encoding: 'utf8',
  check: false,
})
if (changed.status !== 0) {
  process.stderr.write(changed.stderr)
  process.exit(changed.status ?? 1)
}

const files = changed.stdout
  .split('\n')
  .filter(path => /^frontend\/.+\.(?:ts|tsx|css)$/.test(path))
  .map(path => path.slice('frontend/'.length))

if (files.length > 0) {
  const checked = spawnSync('npx', ['biome', 'lint', '--no-errors-on-unmatched', ...files], {
    cwd: frontendRoot,
    stdio: 'inherit',
  })
  process.exit(checked.status ?? 1)
}
