# 林道巡护 · 倒木离线采集系统

巡护员在林道上（无信号）发现倒木时，用浏览器记录经纬度、拍摄现场照片，先存本机；
回站联网后一键补传，坐标与原照写入 SQLite，供清障队在网页上查看并安排清障。

## 功能与实现

| 需求 | 实现 |
| --- | --- |
| 首次在线打开后，断网刷新也能填写 | Service Worker 安装时缓存应用外壳（页面/JS/CSS/样例照片），断网时导航请求回退到缓存 |
| 待传记录不丢 | 记录（含照片 Blob）存入浏览器 IndexedDB，刷新、关机重启均在 |
| 回网点补传，坐标+原照入 SQLite | `POST /api/records`（multipart），照片落盘、元数据入库 |
| 收到确认才移出待传 | 浏览器仅在收到服务器 `{"ok": true}` 响应后才删除 IndexedDB 中的该笔 |
| 确认丢失，重传只入库一次 | 每笔记录保存时生成 UUID 作幂等键；`records.uuid` 唯一约束，重复提交返回 `duplicate: true` 直接确认，不重复写行、不重复存照片 |
| 入库后才供清障队查看 | `/records` 页面只渲染 SQLite 中已确认的记录 |
| 原照写盘后、入库前退出不残留 | 原照先写 `staging/`，入库成功后才原子移入 `photos/`；重启时删除未入库的暂存文件，并把“已入库但未移动”的暂存原照恢复到位 |
| 入库失败清理本次照片 | 写盘或入库异常时删除本次暂存照片，浏览器因未收到确认而保留待传记录，可再次补传 |
| 未入库照片不可访问 | `/photos/<name>` 先查 `records` 表，只有已入库记录引用的原照才返回；孤儿/暂存照片一律 404 |
| 清理不误删 | 已入库记录引用的原照始终保留；本进程仍在写盘的暂存文件不会被清理 |
| 随附样例照片 | `app/static/sample-photo.png`（演示时无相机可点"使用样例照片"，离线也能取到） |

## 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>（Service Worker 要求 localhost 或 HTTPS 环境）。

SQLite 与已入库原照持久化在命名卷 `patrol-data`（容器内 `/data`）。
补传过程中的暂存原照放在 `/data/staging`，只有入库成功才会移入 `/data/photos`；
服务重启时会自动清理未入库的暂存文件（`docker compose restart` / 重新 `up` 后同样生效）。

## 演示：断网采集 → 回网补传

1. **首次在线打开** `http://localhost:8000`，页面右下角无提示即已就绪（Service Worker 已缓存全部外壳资源）。
2. **模拟断网**（任选其一）：
   - 浏览器 DevTools → Network → 勾选 **Offline**；或
   - `docker compose stop`（更真实地模拟"林道上没信号"）。
3. **刷新页面** —— 页面照常打开。填写经纬度（或点"获取当前定位"），选择照片
   （演示可点"使用随附样例照片"），点 **保存到待传记录**。可多采几笔，再次刷新待传列表仍在。
4. **恢复网络**：取消 Offline，或 `docker compose start`。
5. 点 **📤 补传全部待传记录** —— 每笔收到服务器确认后才从待传列表移除。
6. 打开 **/records**（清障队查看页）即可看到已入库的坐标与原照。

**验证幂等**：补传途中若在服务器写入后断网（确认丢失），该笔仍留在待传；
再次补传时服务器按 UUID 判重，返回 `duplicate: true`，浏览器正常移出待传，
SQLite 中始终只有一行。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
DATA_DIR=./data python3 app/app.py        # 或：gunicorn --chdir app -b 0.0.0.0:8000 app:app
```

## 目录结构

```
├── docker-compose.yml      # 一键启动，卷持久化 /data
├── Dockerfile              # python:3.12-slim + gunicorn
├── requirements.txt
├── app/
│   ├── app.py              # Flask 路由 + SQLite（uuid 唯一约束幂等、暂存/清理）
│   ├── templates/
│   │   ├── index.html      # 采集页（巡护员）
│   │   └── records.html    # 已入库记录页（清障队）
│   └── static/
│       ├── app.js          # IndexedDB 待传队列 + 补传逻辑
│       ├── sw.js           # Service Worker（离线外壳缓存）
│       ├── style.css
│       └── sample-photo.png# 随附样例照片
└── tools/
    └── make_sample_photo.py# 样例照片生成脚本（纯标准库）
```

## API

- `GET  /api/records` — 已入库记录 JSON 列表
- `POST /api/records` — 补传（multipart：`uuid`, `lat`, `lng`, `note`, `created_at`, `photo`）
  - 成功：`{"ok": true, "duplicate": false, "id": N}`
  - 重复（同一 uuid）：`{"ok": true, "duplicate": true, "id": N}` — 浏览器同样视为确认
  - 入库/写盘失败：`500 {"ok": false, "error": "..."}` — 本次暂存照片被清理，浏览器保留待传
  - 校验失败：`400 {"ok": false, "error": "..."}`
- `GET  /photos/<file>` — 仅可打开已入库记录引用的原照；未入库文件返回 404
