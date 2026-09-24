#!/usr/bin/env bash
# 前端样式字面量门禁（M2.1 规格 §4.1）：`frontend/src/**/*.vue` 不得出现十六进制颜色字面量
# 或 `@media` 查询，颜色与断点只能来自 src/design/ 的 token 与 Tailwind 工具类。只扫描
# `.vue`；src/design/app.css 中尊重“减少动态效果”的 `@media` 是有意保留的全局规则。
# 默认只打印警告并以 0 退出（绞杀式迁移期间旧 scoped CSS 仍在）；`--strict` 在任一命中时
# 以 1 退出，由 Task 16 在清理完成后接入 `just check`。只读、不联网，可在任意目录运行。
set -euo pipefail
# 固定 C 语言环境：排序、去重与统计与本机区域设置无关，CI 与本地输出逐字一致。
export LC_ALL=C

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_dir="$(cd -- "${script_dir}/../frontend/src" && pwd)"

strict=false
case "${1-}" in
  '') ;;
  --strict) strict=true ;;
  *)
    printf 'usage: %s [--strict]\n' "${0##*/}" >&2
    exit 2
    ;;
esac
[[ "$#" -le 1 ]] || {
  printf 'usage: %s [--strict]\n' "${0##*/}" >&2
  exit 2
}

# 输出 `相对路径:行号:命中文本`（PCRE 语法，需要支持 -P 的 GNU grep；不支持时 grep 以 2
# 退出，按读取错误失败而不是静默放行）；grep 无命中时返回 1，属于正常结果。
scan() {
  local pattern="$1"
  local status=0
  (cd -- "${source_dir}" && grep -rnoP --include='*.vue' -- "${pattern}" .) || status=$?
  if ((status > 1)); then
    printf 'frontend styles failed: cannot scan %s\n' "${source_dir}" >&2
    exit 1
  fi
}

# 十六进制颜色：`#` 后 8、6、4 或 3 位十六进制并以单词边界结束，覆盖 #fff、#1e293b 与带透明度
# 的 #rrggbbaa，也命中 `bg-[#fff]`、`fill="#fff"`、`color:#fff`；前面紧跟 `&` 的是 HTML 实体
# （如 `&#8212;`），不是颜色。
colour_hits="$(scan '(?<!&)#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b')"
# 不要求 `@media` 后有空格：旧 scoped CSS 中存在压缩写法 `@media(max-width:800px)`。
media_hits="$(scan '@media')"

count_lines() {
  [[ -z "$1" ]] && { printf '0'; return; }
  wc -l <<<"$1" | tr -d '[:space:]'
}

colour_count="$(count_lines "${colour_hits}")"
media_count="$(count_lines "${media_hits}")"
distinct_colours=0
if ((colour_count > 0)); then
  # 按字面量（不区分大小写）去重，得到仍需映射为 token 的颜色种类数。
  distinct_colours="$(awk -F: '{ print tolower($NF) }' <<<"${colour_hits}" | sort -u | wc -l | tr -d '[:space:]')"
fi

label='warning'
[[ "${strict}" == true ]] && label='error'
printf 'frontend styles: %s hex colour literal(s) (%s distinct) and %s @media quer(ies) in src/**/*.vue\n' \
  "${colour_count}" "${distinct_colours}" "${media_count}"
if ((colour_count > 0)); then
  # 按文件汇总命中次数与颜色种类（先排序再聚合，保证 CI 日志稳定），避免数十行逐条输出。
  awk -F: '{ printf "%s\t%s\n", substr($1, 3), tolower($NF) }' <<<"${colour_hits}" |
    sort |
    awk -F'\t' -v label="${label}" '
      function flush() {
        if (file != "") printf "  %s: %s: %d hex colour literal(s):%s\n", label, file, count, list
      }
      $1 != file { flush(); file = $1; count = 0; list = ""; last = "" }
      { count += 1; if ($2 != last) { list = list " " $2; last = $2 } }
      END { flush() }
    '
fi
if ((media_count > 0)); then
  while IFS=: read -r file line _; do
    printf '  %s: %s:%s: @media query\n' "${label}" "${file#./}" "${line}"
  done <<<"${media_hits}"
fi

if ((colour_count + media_count == 0)); then
  printf 'frontend styles: OK\n'
elif [[ "${strict}" == true ]]; then
  printf 'frontend styles failed: replace literals with src/design tokens and Tailwind utilities\n' >&2
  exit 1
else
  printf 'frontend styles: warning only (use --strict to fail); legacy styles are removed page by page during M2.1\n'
fi
