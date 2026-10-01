/// <reference types="vite/client" />
// PrimeVue Forms 的 Form、FormField 不在 primevue/config 的类型引用链上，须显式引入其全局组件类型。
/// <reference types="@primevue/forms" />

// 模板中按需注册组件的 Props 类型来自各包自带的 GlobalComponents 声明：PrimeVue 核心组件
// 由 `primevue/config` 的类型文件引入（它引用了全部核心组件模块，main.ts 与
// src/design/primevue.ts 导入了它）；PrimeVue Forms 的 Form、FormField 由上面对
// `@primevue/forms` 的引用引入。已验证：没有 components.d.ts 时 `<Button :label="123" />`、
// `<Form :validate-on-blur="123" />` 与 `<FormField :name="456" />` 均会报错。
// frontend/components.d.ts 只是 unplugin-vue-components 生成的自动导入登记表，已加入
// .gitignore，不是类型安全的前提；因此不在这里用 `/// <reference path>` 引入（全新检出时
// 文件不存在会直接报错），而由 tsconfig.app.json 的 include 在文件存在时顺带收录。
