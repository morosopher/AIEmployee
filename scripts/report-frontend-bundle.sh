#!/usr/bin/env bash
# 前端首屏体积报告与预算门禁（M2.1 规格 §11）：读取已构建的 frontend/dist，把 dist/index.html
# 直接引用的入口 `<script type="module">`、`<link rel="modulepreload">`（首屏 JS）与
# `<link rel="stylesheet">`（首屏 CSS）逐个用 Node zlib 默认级别 gzip，与 Vite
# reportCompressedSize 的口径一致；kB 按 1000 字节计，与构建日志相同。
# 默认只报告；`--budget` 时 JS 相对 M2.1 前基线的 gzip 增量超过 FRONTEND_JS_BUDGET_KB
# （默认 200）或 CSS gzip 总量超过 FRONTEND_CSS_BUDGET_KB（默认 60）即以 1 退出。
# 本脚本不执行构建：`just ci` 已先运行 `pnpm --dir frontend build`。FRONTEND_DIST_DIR 可指向
# 其他构建产物（例如基线工作树），默认是仓库内 frontend/dist。只读、不联网。
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
dist_dir="${FRONTEND_DIST_DIR:-${script_dir}/../frontend/dist}"

budget=false
case "${1-}" in
  '') ;;
  --budget) budget=true ;;
  *)
    printf 'usage: %s [--budget]\n' "${0##*/}" >&2
    exit 2
    ;;
esac
[[ "$#" -le 1 ]] || {
  printf 'usage: %s [--budget]\n' "${0##*/}" >&2
  exit 2
}

if [[ ! -f "${dist_dir}/index.html" ]]; then
  printf 'frontend bundle failed: %s/index.html is missing; run `pnpm --dir frontend build` first\n' \
    "${dist_dir}" >&2
  exit 1
fi

# 解析 HTML、计算 gzip 与预算比较交给 Node：与构建工具链使用同一 zlib 实现，结果可复现。
node - "${dist_dir}" "${budget}" <<'NODE'
'use strict'
const fs = require('node:fs')
const path = require('node:path')
const zlib = require('node:zlib')

const [distArgument, budgetFlag] = process.argv.slice(2)
const distDir = path.resolve(distArgument)
const enforceBudget = budgetFlag === 'true'

/**
 * M2.1 之前的首屏基线：提交 4ce84ae（docs: approve M2.1 frontend refactor design and plan）
 * 以相同口径构建并测量，入口 JS gzip 140373 字节、CSS gzip 2369 字节。规格要求的是
 * 相对该基线的 JS 增量预算，基线变更必须同步修改这里并在提交说明中记录。
 */
const BASELINE_COMMIT = '4ce84ae'
const BASELINE_JS_GZIP_BYTES = 140373
const BASELINE_CSS_GZIP_BYTES = 2369

/** 读取正数预算（kB）；非法值直接失败，避免空值或拼写错误悄悄关闭门禁。 */
function budgetKilobytes(name, fallback) {
  const raw = process.env[name]
  if (raw === undefined || raw === '') return fallback
  if (!/^\d+(\.\d+)?$/.test(raw) || Number(raw) <= 0) {
    console.error(`frontend bundle failed: ${name} must be a positive number of kB, got: ${raw}`)
    process.exit(1)
  }
  return Number(raw)
}
const jsBudgetKb = budgetKilobytes('FRONTEND_JS_BUDGET_KB', 200)
const cssBudgetKb = budgetKilobytes('FRONTEND_CSS_BUDGET_KB', 60)

/** 取标签中某个属性的值；Vite 生成的 HTML 使用双引号，单引号也一并兼容。 */
function attribute(tag, name) {
  const match = new RegExp(`\\s${name}\\s*=\\s*("([^"]*)"|'([^']*)')`, 'i').exec(tag)
  return match ? (match[2] ?? match[3]) : null
}

const html = fs.readFileSync(path.join(distDir, 'index.html'), 'utf8')
const assets = { js: [], css: [] }
for (const [tag] of html.matchAll(/<script\b[^>]*>/gi)) {
  const src = attribute(tag, 'src')
  if (src && attribute(tag, 'type') === 'module') assets.js.push(src)
}
for (const [tag] of html.matchAll(/<link\b[^>]*>/gi)) {
  const rel = (attribute(tag, 'rel') ?? '').toLowerCase().split(/\s+/)
  const href = attribute(tag, 'href')
  if (!href) continue
  if (rel.includes('modulepreload')) assets.js.push(href)
  else if (rel.includes('stylesheet')) assets.css.push(href)
}
if (assets.js.length === 0) {
  console.error('frontend bundle failed: dist/index.html has no module entry script')
  process.exit(1)
}

/** 把 HTML 中的站内 URL 映射到 dist 下的文件；外部地址或越界路径直接失败。 */
function measure(url) {
  if (/^(?:[a-z][a-z0-9+.-]*:)?\/\//i.test(url)) {
    console.error(`frontend bundle failed: first-screen asset is not bundled locally: ${url}`)
    process.exit(1)
  }
  const relative = decodeURIComponent(url.split(/[?#]/)[0]).replace(/^\/+/, '')
  const file = path.resolve(distDir, relative)
  if (!file.startsWith(distDir + path.sep) || !fs.existsSync(file)) {
    console.error(`frontend bundle failed: ${url} does not exist inside ${distDir}`)
    process.exit(1)
  }
  const content = fs.readFileSync(file)
  return { name: relative, raw: content.length, gzip: zlib.gzipSync(content).length }
}

const kb = (bytes) => (bytes / 1000).toFixed(2)
const measured = {
  js: [...new Set(assets.js)].map(measure),
  css: [...new Set(assets.css)].map(measure),
}
const total = (kind) => measured[kind].reduce((sum, asset) => sum + asset.gzip, 0)
const jsGzip = total('js')
const cssGzip = total('css')
const jsIncrement = jsGzip - BASELINE_JS_GZIP_BYTES

for (const kind of ['js', 'css']) {
  for (const asset of measured[kind]) {
    console.log(`  ${kind.padEnd(3)} ${asset.name}  ${kb(asset.raw)} kB raw, ${kb(asset.gzip)} kB gzip`)
  }
}
console.log(
  `frontend bundle: first-screen JS gzip ${kb(jsGzip)} kB (${jsGzip} B, ${measured.js.length} file(s)); ` +
    `CSS gzip ${kb(cssGzip)} kB (${cssGzip} B, ${measured.css.length} file(s))`,
)
console.log(
  `frontend bundle: baseline ${BASELINE_COMMIT} JS gzip ${kb(BASELINE_JS_GZIP_BYTES)} kB, ` +
    `CSS gzip ${kb(BASELINE_CSS_GZIP_BYTES)} kB; JS increment ${jsIncrement >= 0 ? '+' : ''}${kb(jsIncrement)} kB ` +
    `(budget ${jsBudgetKb} kB), CSS ${kb(cssGzip)} kB (budget ${cssBudgetKb} kB)`,
)

if (!enforceBudget) {
  console.log('frontend bundle: report only (use --budget to enforce)')
  process.exit(0)
}
const failures = []
if (jsIncrement > jsBudgetKb * 1000) {
  failures.push(`first-screen JS gzip increment ${kb(jsIncrement)} kB exceeds ${jsBudgetKb} kB`)
}
if (cssGzip > cssBudgetKb * 1000) {
  failures.push(`first-screen CSS gzip ${kb(cssGzip)} kB exceeds ${cssBudgetKb} kB`)
}
if (failures.length) {
  for (const failure of failures) console.error(`frontend bundle failed: ${failure}`)
  process.exit(1)
}
console.log('frontend bundle: budget OK')
NODE
