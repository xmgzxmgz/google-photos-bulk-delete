# Google Photos 批量删除脚本（google-photos-bulk-delete）

用 Playwright 接管你已经登录的 Chrome，自动化批量删除 Google 相簿里的照片/视频。
核心思路是利用 Google 相簿 **「日期分组头部复选框」**（点 1 次 = 选中那一整天所有照片），
比逐张/逐屏勾选的开源工具快得多。

> ⚠️ **删除 = 移动到回收站**，60 天内可在 photos.google.com 回收站恢复；回收站里再「清空回收站」才是永久删除。
> 脚本只做「移到回收站」，不会自动永久删除。

---

## 为什么不用官方 API

Google Photos Library API **至今不提供删除能力**（[issue 109759781](https://issuetracker.google.com/issues/109759781) 从 2019 年挂到现在）。
所以任何「批量删除」工具本质都一样——浏览器自动化模拟点击，本脚本也不例外。
区别只在「怎么选得更快、更稳」。

## 与同类开源工具对比

| 工具 | 选择方式 | 特点 |
| --- | --- | --- |
| mrishab/google-photos-delete-tool | 控制台 JS，逐屏可见照片勾选 | 老牌，但慢、Google 改版易失效 |
| shtse8/Google-Photos-Delete-Tool | Chrome 扩展 / 用户脚本 | 功能全（暂停/统计/重试），但仍是逐张勾选思路 |
| **本脚本** | **日期分组复选框（1 次 = 一整天）** | 大库效率高；每次点击校验计数、无效日期拉黑，避免「勾上又取消」 |

本脚本额外做的健壮性处理（实跑踩坑总结）：
- 删除确认对话框的遮罩会拦截对顶栏按钮的二次点击 → 确认按钮严格限定在 `role=dialog` 内 `force` 点击 + JS 兜底。
- 每次点击日期复选框后校验选中计数是否真的增长，没涨就拉黑该日期（再点 = 取消勾选）。
- 删完一批后页面回填较慢，连续 3 轮「未选中且页面确无瓦片」才判定图库已空，避免提前结束。

---

## 功能特性

- 自动探测 Chrome 可执行文件、User Data 目录（Windows / macOS / Linux 均可）。
- 用「非默认目录名的 junction」绕过 Chrome 拒绝在默认用户目录开调试端口的限制，登录态完好。
- 安全闸：默认 `DRY_RUN`（只选不删），需显式放开才会真正删除。
- `Ctrl+C` 随时中止；每批之间可调停顿，降低风控概率。
- 全部路径 / 行为通过环境变量配置，无硬编码。

---

## 安全警告（务必先读）

1. **删除不可逆到回收站级别**：移到回收站后 60 天内可恢复，但请先确认已备份。
2. **默认不删除**：`GPBD_DRY_RUN` 默认开启，脚本只会选中、不会删。请先 DRY_RUN 验证能进图库、能选中大量照片，再放开删除。
3. **全量删除前先试删一小批**：先 `GPBD_FULL_DELETE=0` + `GPBD_BACKUP_DONE=1` 试删 10 张确认无误，再开 `GPBD_FULL_DELETE=1`。
4. 本工具只操作**你自己的** Google 相簿账号，请合法、合规使用。

---

## 安装

需要 Python 3.8+ 和本机已安装 Chrome。

```bash
pip install playwright
playwright install chromium
```

（脚本自己启动系统 Chrome 走 CDP，安装 chromium 只是为了让 Playwright 库可用；实际驱动的是你本机 Chrome。）

---

## 配置（环境变量，均可选）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GPBD_CHROME_EXE` | 自动探测 | Chrome 可执行文件路径 |
| `GPBD_CHROME_USER_DATA` | 按平台自动推断 | 真实 User Data 目录（含登录态） |
| `GPBD_CHROME_PROFILE` | `Default` | 多账号时改 `Profile 1` / `Profile 2` |
| `GPBD_CHROME_LINK` | 脚本目录下 `chrome_link` | junction 目录（绕过默认目录限制） |
| `GPBD_DRY_RUN` | `1` | `0` = 关闭 dry-run，进入真实删除准备 |
| `GPBD_BACKUP_DONE` | `0` | `1` = 确认已备份相册 |
| `GPBD_FULL_DELETE` | `0` | `1` = 全量删除 |
| `GPBD_TEST_BATCH` | `10` | 试删阶段累计上限 |
| `GPBD_BATCH_TARGET` | `2000` | 每轮累积选中并一次删除的目标张数 |
| `GPBD_BATCH_PAUSE` | `1.0` | 每批之间停顿（秒） |
| `GPBD_CLICK_PAUSE` | `0.08` | 选中单张之间的停顿（秒） |

---

## 使用步骤

1. **彻底关闭 Chrome**（含后台进程）：
   ```bash
   # Windows
   taskkill /F /IM chrome.exe
   # macOS / Linux
   pkill -f "Google Chrome"
   ```

2. **先 DRY_RUN 验证**（默认就是 DRY_RUN，直接跑）：
   ```bash
   python google_photos_delete.py
   ```
   脚本会进图库、选中大量照片、截图 `step2_selected.png`，但**不会删除**。请肉眼确认勾选正常。

3. **试删一小批**（确认选择逻辑 OK 后）：
   ```bash
   GPBD_DRY_RUN=0 GPBD_BACKUP_DONE=1 GPBD_FULL_DELETE=0 python google_photos_delete.py
   ```
   会删除约 `GPBD_TEST_BATCH`（默认 10）张，验证删除确认链路。

4. **全量删除**：
   ```bash
   GPBD_DRY_RUN=0 GPBD_BACKUP_DONE=1 GPBD_FULL_DELETE=1 python google_photos_delete.py
   ```
   按一次 `Enter` 开始；之后每批自动进行，随时 `Ctrl+C` 可停。

5. **彻底清空**（可选）：去 photos.google.com 回收站 → 「清空回收站」。

---

## 工作原理

1. 用「真实 Chrome + 真实 User Data」启动一个带 `--remote-debugging-port` 的实例（junction 绕过默认目录限制）。
2. Playwright `connect_over_cdp` 接管该实例的已登录页面。
3. 进入选择态后，逐个点击「未勾选的日期分组复选框」累积选中（每点 1 个 = 一整天），累计到 `BATCH_TARGET` 或没有更多日期为止。
4. 点顶栏「移至回收站」打开确认对话框 → 在 `role=dialog` 内 `force` 点击确认按钮。
5. 轮询选择计数归零判定删除成功；刷新页面进入下一轮，直到图库清空。

---

## 已知限制

- 完全依赖 photos.google.com 的 DOM 结构，Google 改版可能导致选择器失效，需要跟进。
- 只能「移到回收站」，不是永久删除。
- 大批量仍需反复滚动加载，整体耗时取决于网速与照片数量。
- 中国大陆访问 Google 服务需自备代理；脚本会自动读取系统代理设置，失败时回退常见 Clash 端口（7897 / 7890 / 1080）。

---

## License

[MIT](./LICENSE)
