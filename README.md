# Socket FTP Client

本项目按 `document` 文件夹中的软件设计要求选择 FTP 客户端方向实现：

- 图形化界面：使用 Python 标准库 `tkinter`。
- 协议实现：不使用 `ftplib`，从 `socket.create_connection` 建立 TCP 控制连接开始实现 FTP 命令。
- 文件列表：支持 `EPSV/PASV + LIST`。
- 下载：支持 `REST + RETR` 断点续传。
- 上传：支持 `SIZE + APPE/STOR` 断点续传。

## 运行方式

Windows 或 macOS/Linux 均可运行，Windows 符合课程要求的客户端操作系统说明。

```bash
python src/ftp_client.py
```

程序仅依赖 Python 标准库。启动后填写 FTP 服务器地址、端口、用户名和密码，连接成功后即可双击远程目录进入，选择文件上传或下载。

## 本地示例 FTP 服务器

如果没有现成 FTP 服务器，可以先启动项目自带的本地测试服务器：

```bash
python tools/demo_ftp_server.py
```

然后在客户端中填写：

- Host: `127.0.0.1`
- Port: `2121`
- User: 任意内容
- Password: 任意内容

测试服务器的根目录默认是 `demo_ftp_root/`，支持目录列表、上传、下载和断点续传。它只用于本地实验，不要作为公网服务使用。

## 目录

- `src/ftp_client.py`：FTP 客户端源码。
- `tools/demo_ftp_server.py`：本地示例 FTP 服务器。
- `tools/generate_report.py`：根据实验报告模板生成报告初稿。
- `report/计网-xx组.docx`：生成后的实验报告初稿。
