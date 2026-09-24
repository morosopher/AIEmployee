#!/usr/bin/env bash
# 前端依赖许可证与 PrimeVue 主版本门禁（M2.1 规格 §3.1、§3.2）：生产依赖只允许 MIT、ISC、
# BSD-2-Clause、BSD-3-Clause、Apache-2.0，开发依赖额外允许 MPL-2.0；PrimeVue 相关包必须精确
# 锁定 4.5.5，并拒绝 PrimeVue 5、@primeui/* 与 @primeuix 商业许可产品线。
# 只读取 package.json、pnpm-lock.yaml 和 pnpm 对已安装依赖的许可证清单：不安装、不联网、
# 不修改文件；需先执行 `pnpm --dir frontend install --frozen-lockfile`。可在任意目录运行。
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
frontend_dir="$(cd -- "${script_dir}/../frontend" && pwd)"

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/ai-employee-frontend-licenses.XXXXXX")"
cleanup() {
  rm -rf -- "${work_dir}"
}
trap cleanup EXIT

# 生产与开发依赖树分别取清单，按各自策略判定；pnpm 读取失败时 fail-closed，绝不视为通过。
for scope in prod dev; do
  if ! pnpm --dir "${frontend_dir}" licenses list "--${scope}" --json \
    >"${work_dir}/${scope}.json" 2>"${work_dir}/${scope}.err"; then
    printf 'frontend licenses failed: pnpm licenses list --%s did not succeed\n' "${scope}" >&2
    cat -- "${work_dir}/${scope}.err" >&2
    exit 1
  fi
done

# JSON 与 SPDX 表达式交给 Node 解析（仓库不依赖 jq）；全部问题汇总输出后再以非零退出。
node - "${work_dir}/prod.json" "${work_dir}/dev.json" \
  "${frontend_dir}/package.json" "${frontend_dir}/pnpm-lock.yaml" <<'NODE'
'use strict'
const fs = require('node:fs')

const [prodPath, devPath, manifestPath, lockPath] = process.argv.slice(2)

/** PrimeVue 4.x 最后一个 MIT 发布线；升级必须经 ADR 批准后同步修改这里。 */
const PRIMEVUE_VERSION = '4.5.5'

/** 生产依赖允许的 SPDX 标识；开发依赖在此基础上额外允许 MPL-2.0。 */
const PROD_ALLOWED = new Set(['MIT', 'ISC', 'BSD-2-Clause', 'BSD-3-Clause', 'Apache-2.0'])
const DEV_ALLOWED = new Set([...PROD_ALLOWED, 'MPL-2.0'])

/**
 * M2.1 之前（提交 4ce84ae）就已存在的许可证例外，按“包名 + pnpm 报告的许可证原文”精确
 * 匹配；新增依赖不得借用。生产例外同样适用于开发依赖树，开发例外只适用于开发依赖树。
 */
const PROD_EXCEPTIONS = [
  // markdown-it 的传递依赖，Python-2.0 为宽松许可（OSI 批准），仅用于解析命令行参数。
  { name: 'argparse', license: 'Python-2.0' },
]
const DEV_EXCEPTIONS = [
  // jsdom/CSS 解析链的传递依赖，MIT-0 是去掉署名要求的 MIT，比 MIT 更宽松。
  { name: '@csstools/color-helpers', license: 'MIT-0' },
  { name: '@csstools/css-syntax-patches-for-csstree', license: 'MIT-0' },
  // glob/minimatch 工具链的传递依赖，BlueOak-1.0.0 是 OSI 批准的宽松许可。
  { name: 'jackspeak', license: 'BlueOak-1.0.0' },
  { name: 'lru-cache', license: 'BlueOak-1.0.0' },
  { name: 'minimatch', license: 'BlueOak-1.0.0' },
  { name: 'minipass', license: 'BlueOak-1.0.0' },
  { name: 'package-json-from-dist', license: 'BlueOak-1.0.0' },
  { name: 'path-scurry', license: 'BlueOak-1.0.0' },
  // css-tree 的 CSS 规范数据集，CC0-1.0 为公有领域贡献，只在测试环境使用。
  { name: 'mdn-data', license: 'CC0-1.0' },
]

const failures = []

/**
 * 求值 SPDX 许可证表达式：OR 任一分支允许即通过，AND 要求全部允许，支持括号。
 * `WITH` 例外条款、`+` 后缀、`SEE LICENSE IN …`、Unknown 或任何无法解析的写法一律不通过。
 */
function spdxAllowed(expression, allowed) {
  const tokens = expression.replace(/[()]/g, ' $& ').trim().split(/\s+/).filter(Boolean)
  let index = 0
  const operators = new Set(['AND', 'OR', 'WITH', '(', ')'])
  function atom() {
    const token = tokens[index++]
    if (token === '(') {
      const value = disjunction()
      if (tokens[index++] !== ')') throw new Error('unbalanced parentheses')
      return value
    }
    if (token === undefined || operators.has(token)) throw new Error('unexpected token')
    if (tokens[index] === 'WITH') throw new Error('license exception clause needs review')
    return allowed.has(token)
  }
  function conjunction() {
    let value = atom()
    while (tokens[index] === 'AND') {
      index += 1
      value = atom() && value
    }
    return value
  }
  function disjunction() {
    let value = conjunction()
    while (tokens[index] === 'OR') {
      index += 1
      value = conjunction() || value
    }
    return value
  }
  try {
    const value = disjunction()
    return index === tokens.length && value
  } catch {
    return false
  }
}

/** 按依赖树检查每个包版本；返回用于摘要的统计与实际使用的例外。 */
function checkScope(label, path, allowed, exceptions) {
  const report = JSON.parse(fs.readFileSync(path, 'utf8'))
  const counts = new Map()
  const usedExceptions = []
  let total = 0
  for (const [license, packages] of Object.entries(report)) {
    for (const pkg of packages) {
      const versions = Array.isArray(pkg.versions) && pkg.versions.length ? pkg.versions : ['?']
      for (const version of versions) {
        total += 1
        const id = `${pkg.name}@${version}`
        if (spdxAllowed(license, allowed)) {
          counts.set(license, (counts.get(license) ?? 0) + 1)
        } else if (exceptions.some((entry) => entry.name === pkg.name && entry.license === license)) {
          usedExceptions.push(`${id} (${license})`)
        } else {
          failures.push(`${label} dependency ${id} has disallowed license: ${license}`)
        }
      }
    }
  }
  const breakdown = [...counts].map(([license, count]) => `${license} ${count}`).join(', ')
  console.log(`frontend licenses: ${label} ${total} package versions checked (${breakdown})`)
  if (usedExceptions.length) {
    console.log(`  baseline exceptions (${label}): ${usedExceptions.join(', ')}`)
  }
  // 空清单通常意味着依赖未安装或 pnpm 输出异常；“0 个包全部合规”不能当作通过。
  if (total === 0) {
    failures.push(`${label} license listing is empty; run pnpm --dir frontend install --frozen-lockfile first`)
  }
}

checkScope('prod', prodPath, PROD_ALLOWED, PROD_EXCEPTIONS)
checkScope('dev', devPath, DEV_ALLOWED, [...PROD_EXCEPTIONS, ...DEV_EXCEPTIONS])

// 直接依赖必须写精确版本：范围符号会让未来安装悄悄滑向新的发布线。
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'))
const declared = { ...manifest.dependencies, ...manifest.devDependencies }
for (const name of ['primevue', '@primevue/forms', '@primevue/auto-import-resolver']) {
  if (declared[name] !== PRIMEVUE_VERSION) {
    failures.push(`package.json must pin ${name} to exactly ${PRIMEVUE_VERSION}, found: ${declared[name] ?? 'missing'}`)
  }
}
if (!/^~2\.\d+\.\d+$/.test(declared['@primeuix/themes'] ?? '')) {
  failures.push(`package.json must lock @primeuix/themes to a 2.x minor (~2.y.z), found: ${declared['@primeuix/themes'] ?? 'missing'}`)
}

/**
 * 判断锁文件解析出的单个包版本是否违反主版本锁定，返回拒绝原因；合规时返回 null。
 * PrimeVue 全家必须恰为 4.5.5；@primeuix 的 themes 3.x、styled 1.x、styles 3.x、
 * utils 0.8+ 与 PrimeVue 5 同步改为 PrimeUI 商业许可，@primeui/* 全部是商业产品。
 */
function forbiddenReason(name, major, minor, version) {
  if ((name === 'primevue' || name.startsWith('@primevue/')) && version !== PRIMEVUE_VERSION) {
    return `PrimeVue packages must resolve to exactly ${PRIMEVUE_VERSION}; 5.x is PrimeUI-licensed and other 4.x releases need an ADR`
  }
  if (name.startsWith('@primeui/')) return 'PrimeUI commercial package'
  if (name === '@primeuix/themes' && major >= 3) return 'commercial @primeuix/themes 3.x line'
  if (name === '@primeuix/styled' && major >= 1) return 'commercial @primeuix/styled 1.x line'
  if (name === '@primeuix/styles' && major >= 3) return 'commercial @primeuix/styles 3.x line'
  if (name === '@primeuix/utils' && (major >= 1 || minor >= 8)) return 'commercial @primeuix/utils 0.8+ line'
  return null
}

// 扫描锁文件中实际解析的每个包（packages 与 snapshots 两节都会列出同一包，结果按 Set 去重）。
const lock = fs.readFileSync(lockPath, 'utf8')
const resolved = new Set()
const lockFailures = new Set()
for (const line of lock.split('\n')) {
  const match = /^ {2}'?((?:@[^/@'\s]+\/)?[^@'\s/]+)@(\d+)\.(\d+)\.(\d+)[^'\s:]*'?:\s*$/.exec(line)
  if (!match) continue
  const [, name, major, minor, patch] = match
  const version = `${major}.${minor}.${patch}`
  resolved.add(`${name}@${version}`)
  const reason = forbiddenReason(name, Number(major), Number(minor), version)
  if (reason) lockFailures.add(`pnpm-lock.yaml resolves forbidden ${name}@${version}: ${reason}`)
}
failures.push(...lockFailures)
// 兜底文本扫描：即使条目格式变化，任何 @primeui/ 引用（含 license-manager）都不允许出现。
if (lock.includes('@primeui/')) failures.push('pnpm-lock.yaml references the @primeui/ scope')
const primeFamily = [...resolved].filter(
  (id) => id.startsWith('primevue@') || id.startsWith('@primevue/') || id.startsWith('@primeuix/'),
)
if (!primeFamily.some((id) => id === `primevue@${PRIMEVUE_VERSION}`)) {
  failures.push(`pnpm-lock.yaml does not resolve primevue@${PRIMEVUE_VERSION}`)
}
console.log(`frontend licenses: lock resolves ${primeFamily.sort().join(', ')}`)

if (failures.length) {
  for (const failure of failures) console.error(`frontend licenses failed: ${failure}`)
  process.exit(1)
}
console.log('frontend licenses: OK')
NODE
