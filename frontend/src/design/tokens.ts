/**
 * M2.1 设计 token 的唯一来源：语义色板、WCAG 对比度计算和定制 Aura 预设。
 *
 * 组件与页面只能引用这里导出的语义名，或经 PrimeVue CSS 变量与
 * tailwindcss-primeui 暴露的工具类（如 `bg-surface-50`、`text-muted-color`、
 * `border-surface`）；`.vue` 文件不得再写十六进制颜色。界面固定为浅色主题，因此
 * 预设只定制 `colorScheme.light`，暗色方案由 PrimeVue 配置整体关闭。
 */
import { definePreset, palette } from '@primeuix/themes'
import Aura from '@primeuix/themes/aura'

/** PrimeVue 色板的 11 个档位，与 Aura 原始色板一一对应。 */
type ScaleStep = 50 | 100 | 200 | 300 | 400 | 500 | 600 | 700 | 800 | 900 | 950

/** 完整色板：键为档位，值为十六进制颜色或 `{slate.100}` 形式的 token 引用。 */
type ColorScale = Readonly<Record<ScaleStep, string>>

/**
 * M2 scoped CSS 中 15 种不透明硬编码颜色收敛后的语义色板（映射在 Task 1 固定）。另一处
 * 半透明遮罩 `#0f172a80`（ActionConfirmationDialog）不进入色板：规格 §6 把该组件迁移为
 * PrimeVue `Dialog modal`，遮罩改由 Aura 的 `mask.background` token 提供。
 *
 * - `primary`：主按钮、链接、当前导航与焦点环。
 * - `danger`：逾期告警、拒绝、`needs_attention` 与错误文字。
 * - `warn`：工作时间外、部分成功、能力降级；`border` 用于风险标记描边。
 * - `surface`：页面背景、卡片与分隔线；旧值 `#d7dce5`、`#ddd` 视觉上与
 *   `#dce2ea` 无差别，统一并入 `surface.border`，旧值 `#fff` 规范化为 `card`。
 * - `text`：正文、次级文字与禁用文字。`disabled` 仅用于不可交互控件，WCAG 1.4.3
 *   对其豁免对比度要求，不得承载需要阅读的信息。它只按名称导出供显式引用，不写入
 *   PrimeVue 色板：Aura 的 `surface.400` 同时用于可交互元素（输入框悬停描边、下拉图标、
 *   开关悬停轨道等），把禁用色放进该档位会拉低这些元素的对比度。
 * - `tint`：主色与危险色的浅底，分别对应旧的选中行 `#eef5ff` 与错误块 `#fff5f5`。
 *
 * `success` 与 `info` 不在此表：它们由 Aura 默认色板派生，见 {@link appPreset}。
 */
export const semanticColors = Object.freeze({
  primary: '#164e9c',
  danger: '#a61b1b',
  warn: Object.freeze({
    foreground: '#946200',
    background: '#fff5df',
    border: '#c6aa72',
  }),
  surface: Object.freeze({
    page: '#f6f8fb',
    card: '#ffffff',
    border: '#dce2ea',
  }),
  text: Object.freeze({
    primary: '#1e293b',
    secondary: '#485365',
    disabled: '#a8b1bf',
  }),
  tint: Object.freeze({
    primary: '#eef5ff',
    danger: '#fff5f5',
  }),
} as const)

/**
 * 解析 `#rgb` 或 `#rrggbb` 为 0~255 的 sRGB 通道。
 *
 * @param colour 十六进制颜色，大小写不敏感。
 * @returns `[r, g, b]` 通道值。
 * @throws RangeError 输入不是三位或六位十六进制颜色时，拒绝猜测命名色或 token 引用。
 */
function parseHexColour(colour: string): [number, number, number] {
  const match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(colour.trim())
  if (!match?.[1]) throw new RangeError(`Unsupported colour value: ${colour}`)
  const digits =
    match[1].length === 3
      ? [...match[1]].map((digit) => digit + digit).join('')
      : match[1]
  return [0, 2, 4].map((offset) =>
    Number.parseInt(digits.slice(offset, offset + 2), 16),
  ) as [number, number, number]
}

/**
 * 按 WCAG 2.1 定义计算相对亮度：先把 sRGB 通道线性化，再按人眼敏感度加权。
 *
 * @param colour 十六进制颜色。
 * @returns 0（黑）到 1（白）之间的相对亮度。
 * @throws RangeError 颜色格式不受支持时。
 */
function relativeLuminance(colour: string): number {
  const [red, green, blue] = parseHexColour(colour).map((channel) => {
    const value = channel / 255
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  }) as [number, number, number]
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue
}

/**
 * 计算两种颜色的 WCAG 2.1 对比度，供 token 单测与后续组件验证复用。
 *
 * 结果与参数顺序无关；正文 AA 阈值为 4.5，大号文字与界面组件边界为 3。本函数
 * 只接受不透明的十六进制颜色，带透明度或 `color-mix()` 的值需要调用方先求出实际
 * 叠加色，避免在无法确定背景时给出虚假的通过结论。
 *
 * @param foreground 前景色（通常是文字）。
 * @param background 背景色。
 * @returns 1 到 21 之间的对比度。
 * @throws RangeError 任一颜色不是 `#rgb`/`#rrggbb` 时。
 */
export function contrastRatio(foreground: string, background: string): number {
  const first = relativeLuminance(foreground)
  const second = relativeLuminance(background)
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05)
}

/**
 * 用 PrimeUI 的 `palette()` 从基色派生完整色板，基色落在 500 档；`anchors` 把旧界面
 * 的精确浅底或描边固定到 PrimeVue 读取它们的档位，使迁移后视觉与旧值一致。
 *
 * @param base 位于 500 档的基色。
 * @param anchors 需要覆盖的档位与旧颜色。
 * @returns 11 档完整色板。
 * @throws Error palette 未返回完整色板时（例如传入了 token 引用）。
 */
function deriveScale(
  base: string,
  anchors: Partial<Record<ScaleStep, string>> = {},
): ColorScale {
  const generated = palette(base)
  if (typeof generated === 'string') {
    throw new Error(`Cannot derive a colour scale from ${base}`)
  }
  const steps: ScaleStep[] = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950]
  return Object.freeze(
    Object.fromEntries(
      steps.map((step) => {
        const value = anchors[step] ?? generated[step]
        if (!value) throw new Error(`Missing ${step} step for ${base}`)
        return [step, value]
      }),
    ) as Record<ScaleStep, string>,
  )
}

/** Aura 默认 green.700 与 sky.700：原 500 档配白字仅约 2.3:1 与 2.8:1，未达 AA。 */
const AURA_GREEN_700 = '#15803d'
const AURA_SKY_700 = '#0369a1'

/** 主色板：500 为品牌主色，50 固定为旧选中行浅底（Aura highlight 背景读取 50 档）。 */
const primaryScale = deriveScale(semanticColors.primary, {
  50: semanticColors.tint.primary,
})

/** 危险色板：Aura 的 danger/error 严重度与表单 invalid 状态都读取 red 色板。 */
const dangerScale = deriveScale(semanticColors.danger, {
  50: semanticColors.tint.danger,
})

/**
 * 警告色板：Tag/Button 读取 orange，Message/Toast 读取 yellow；50 档是旧警告块
 * 浅底，200 档是 Message 描边，与旧风险标记描边一致。
 */
const warnScale = deriveScale(semanticColors.warn.foreground, {
  50: semanticColors.warn.background,
  200: semanticColors.warn.border,
})

/** 成功与信息色板由 Aura 默认 700 档派生，使白字按钮与浅底提示同时满足 AA。 */
const successScale = deriveScale(AURA_GREEN_700)
const infoScale = deriveScale(AURA_SKY_700)

/**
 * 浅色 surface 色板：旧界面的页面背景、卡片、分隔线与正文/次级文字色放在各自自然档位，
 * 其余档位沿用 Aura 默认的 slate 引用，保证从浅到深单调递增。400 档必须保持 Aura 的
 * slate.400：Aura 用它渲染可交互元素，禁用文字色只按名称导出（见 {@link semanticColors}）。
 */
const surfaceScale = Object.freeze({
  0: semanticColors.surface.card,
  50: semanticColors.surface.page,
  100: '{slate.100}',
  200: semanticColors.surface.border,
  300: '{slate.300}',
  400: '{slate.400}',
  500: '{slate.500}',
  600: semanticColors.text.secondary,
  700: '{slate.700}',
  800: semanticColors.text.primary,
  900: '{slate.900}',
  950: '{slate.950}',
} as const)

/**
 * 应用唯一的 PrimeVue 预设：以 Aura 为基础，只替换色板与少量语义/组件 token，
 * 圆角、间距、阴影、动效时长全部沿用 Aura 默认值。
 *
 * - 原始色板：red←danger，orange/yellow←warn，green/sky←Aura 700 档派生。
 *   Aura 默认的 500 档白字按钮与 600 档浅底提示多处低于 4.5:1，替换后 Tag、
 *   Message、Toast、Button 全部严重度均由 `tokens.spec.ts` 用 PrimeVue 解析器核对 AA。
 * - 语义 token：主色板、浅色 surface 色板；正文/次级文字比 Aura 默认各深一档，
 *   分别等于 `text.primary` 与 `text.secondary`。
 * - 非文本对比度（WCAG 2.1 1.4.11，≥ 3:1）：Aura 的输入框描边（surface.300）、悬停描边与
 *   下拉/日期图标（surface.400）、开关未选中轨道（surface.300）在白底上只有约
 *   1.5:1～2.6:1，用户难以辨认控件边界与状态。这里只把它们改到现有 surface 档位，
 *   在卡片（surface.0）与页面（surface.50）底色上都达到 3:1，由 `tokens.spec.ts` 核对。
 * - 不定制暗色方案：暗色由 PrimeVue 配置 `darkModeSelector: false` 关闭。
 */
export const appPreset = definePreset(Aura, {
  primitive: {
    red: dangerScale,
    orange: warnScale,
    yellow: warnScale,
    green: successScale,
    sky: infoScale,
  },
  semantic: {
    primary: primaryScale,
    colorScheme: {
      light: {
        surface: surfaceScale,
        text: {
          color: '{surface.800}',
          hoverColor: '{surface.900}',
          mutedColor: '{surface.600}',
          hoverMutedColor: '{surface.700}',
        },
        formField: {
          // 输入框、下拉框、复选框等的描边标识控件边界：surface.500 在白底约 4.8:1。
          borderColor: '{surface.500}',
          // 悬停描边再深一档，保持与常态可区分，否则悬停反馈会消失或反而变浅。
          hoverBorderColor: '{surface.600}',
          // 下拉箭头与日期图标是识别控件用途的图形，与常态描边同档。
          iconColor: '{surface.500}',
        },
      },
    },
  },
  components: {
    toggleswitch: {
      colorScheme: {
        light: {
          root: {
            // 未选中轨道是开关状态的唯一图形；白色滑块在 surface.500 上同样约 4.8:1。
            background: '{surface.500}',
            // 悬停轨道深一档，避免沿用 Aura 的 surface.400 后悬停时反而变浅。
            hoverBackground: '{surface.600}',
          },
        },
      },
    },
  },
})
