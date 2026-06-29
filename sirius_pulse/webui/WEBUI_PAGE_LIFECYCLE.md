# WebUI 页面生命周期框架

WebUI 使用轻量原生 ES module 路由，不引入前端框架。页面模块统一接收 `PageContext`，避免切页后旧异步任务继续操作已替换的 DOM。

## 页面模块协议

```js
export async function init(container, { ctx, signal }) {
  container.innerHTML = '<button id="save">保存</button>';

  ctx.on(ctx.$('save'), 'click', save);

  const res = await ctx.fetch('/api/example');
  if (!ctx.isActive() || !res) return;

  render(await res.json());
}
```

页面内代码应遵守：

- 使用 `ctx.$()` / `ctx.$$()`，不要用全局 `document.getElementById()` 或组件层全局 `$()` 查页面元素。
- 使用 `ctx.on()` 绑定事件，切页时会自动解绑。
- 使用 `ctx.fetch()`、`ctx.timeout()`、`ctx.interval()` 发起异步任务，切页时会自动取消或静默停止。
- 异步 `await` 返回后，修改 DOM 前检查 `ctx.isActive()`。
- 如需特殊资源清理，页面可继续导出 `destroy()` 或 `dispose()`。

## 旧页面兼容层

已有页面可以先使用 `createScopedPage()` 保持旧式 `$()` 写法，但查询、timer 和全局监听都会进入当前页面生命周期：

```js
import { createScopedPage } from '../page-context.js';

const scopedPage = createScopedPage();
const $ = scopedPage.$;

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  scopedPage.on($('refreshBtn'), 'click', refresh);
  scopedPage.timeout(refresh, 1000);
}
```

切页后，旧页面的 scoped 查询会返回惰性空元素或空列表，避免旧 Promise 回调继续修改新页面或抛出 `null.addEventListener`。

## 路由层保证

`app.js` 每次 `navTo()` 会：

1. `abort()` 上一个页面的 `AbortController`。
2. 调用旧页面模块的 `dispose()` 或 `destroy()`。
3. 为新页面创建新的 `PageContext`。
4. 将 `{ ctx, signal }` 传入页面 `init()`。
5. 丢弃过期导航的 HTML、模块加载和初始化结果。

## 守护测试

`tests/js/page-lifecycle-guard.test.mjs` 防止页面模块重新引入这些风险模式：

- 从 `components.js` 导入全局 `$`。
- 使用 `document.getElementById()`、`document.querySelector()` 或 `document.querySelectorAll()` 查询页面元素。
- 使用未托管的 `setTimeout()` / `setInterval()`。
- 直接在 `window` / `document` 上绑定未托管事件。

这个协议把页面生命周期集中在路由层，保持 WebUI 轻量，同时解决页面多、异步多时的 DOM 竞态问题。

