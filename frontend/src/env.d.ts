/// <reference types="vite/client" />

// 模板中 PrimeVue 组件的 Props 类型来自 PrimeVue 自带的 GlobalComponents 声明：
// `primevue/config` 的类型文件引用了全部组件模块，main.ts 与 src/design/primevue.ts 导入它后
// 全部组件即受类型检查（已验证：没有 components.d.ts 时 `<Button :label="123" />` 同样报错）。
// frontend/components.d.ts 只是 unplugin-vue-components 生成的自动导入登记表，已加入
// .gitignore，不是类型安全的前提；因此不在这里用 `/// <reference path>` 引入（全新检出时
// 文件不存在会直接报错），而由 tsconfig.app.json 的 include 在文件存在时顺带收录。
