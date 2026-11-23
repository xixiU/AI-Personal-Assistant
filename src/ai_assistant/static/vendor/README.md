# static/vendor 第三方库

这些文件提交在仓库里，页面直接引用，不依赖公网 CDN，内网断网环境可以正常使用。

| 文件 | 库 | 版本 | 来源 | 许可 |
|------|----|------|------|------|
| marked.min.js | marked（Markdown 解析） | 15.0.12 | https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js | MIT |

使用方：`static/index.html` 通过 `<script src="/static/vendor/marked.min.js">` 加载。marked 加载失败时，页面会改用内置的正则渲染，基本格式仍能显示。

## 升级方法

1. 在能上网的机器上下载指定版本，例如：
   `curl -o marked.min.js https://cdn.jsdelivr.net/npm/marked@<版本>/marked.min.js`
2. 覆盖本目录下的文件，并更新上表的版本号。
3. 打开聊天页，确认代码块、表格、链接（在新标签页打开）都能正常渲染。index.html 用到了 `marked.Renderer` 和 `renderer.link(token)`，这是 v13 以后的 token 形式，跨大版本升级时要检查这两处。

## 引用路径约定

所有页面一律用以 `/static/` 开头的绝对路径引用 css/js/vendor 文件，不用相对路径。
页面所在层级不同（`/`、`/profile`、`/admin/`、`/admin/users`，管理员路径还可以配置成多级），
相对路径 `css/app.css` 在 `/admin/users` 下会解析成 `/admin/css/app.css`，导致 404；
绝对路径在任何入口下都解析为同一个地址，由后端的 `/static/<path>` 路由提供。
`app.css` 里没有 `url()` 引用，没有额外的路径问题。

前提：服务直接部署在 `ip:port/` 根路径下。如果以后放到反向代理的子路径（如 `/ai/`）下，
需要由代理改写路径，或者统一修改这些引用。

## 页面样式

账号相关页面和管理后台统一使用 `static/css/app.css`。这是手写的 CSS，用 `:root` 变量统一颜色、圆角和阴影，
没有构建步骤和 npm 依赖，修改后刷新页面即可生效。图表由 `static/js/charts.js` 用 SVG 绘制，也没有第三方依赖。

没有引入 Tailwind，原因如下：
- Tailwind v3/v4 不提供预构建的完整 CSS，要么用 npm 构建，要么用浏览器版（Play CDN 脚本）。
  浏览器版在页面加载时编译样式，官方不建议用于生产环境，首屏会出现短暂的无样式闪烁。
- 最后一个提供预构建 CSS 的版本是 v2.2.19，文件约 3MB，而且已停止维护。
- 现有页面都用语义化类名写成，包括 admin-users.js、admin-data.js 里拼出来的表格 HTML。
  改用 Tailwind 需要重写全部页面和这两个脚本，但视觉上的提升有限。

图表只有折线图和条形列表两种，charts.js 已经覆盖。ECharts 等库压缩后约 1MB，暂不引入。

如果以后引入第三方 js/css，放进本目录，在上表登记版本、下载地址和用途，并用 `/static/vendor/...` 绝对路径引用。
