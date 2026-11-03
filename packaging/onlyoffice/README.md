# 本机 ONLYOFFICE 服务

`start-local.sh` 是 Linux 服务主机管理员的准备脚本，不需要业务用户安装运行环境；它将服务放入 Docker `--internal` 私有网络，通过本机 `127.0.0.1:18080` 转发，首次启动生成随机 JWT 密钥，数据保存在 Docker 命名卷中。脚本每次启动动态读取容器地址并重建临时 systemd 转发单元。运行前需要 Docker、systemd、socat、OpenSSL、足够磁盘空间及镜像下载网络，通常以管理员身份运行。默认固定 ONLYOFFICE 10.0 的官方镜像索引摘要 `sha256:ca17889f10b11141cc6441836aebdd2835cf3e1b49681b2fd6e155c94b7850b3`；可以使用 `ATLAS_ONLYOFFICE_IMAGE` 指定管理员已验证来源和摘要的镜像。

停止服务：`systemctl stop atlas-onlyoffice-local-proxy.service`，再执行 `docker stop atlas-onlyoffice`。重启机器后需再次运行启动脚本以重新建立转发。内网隔离不是数据权限管理，也不代表产品接入已完成。

服务健康不代表已经完成生产接入。Atlas 支持按项目和用户授权签发短期一次性只读文件链接，并将查看页面放在独立的 ONLYOFFICE 源；默认关闭，配置错误或服务不可用时回退原有显示。运营人员须为 Atlas 服务配置 `ATLAS_ONLYOFFICE_JWT_SECRET`（与文档服务一致）、`ATLAS_ONLYOFFICE_BROWSER_ORIGIN`（浏览器可访问的文档服务内网源）、`ATLAS_ONLYOFFICE_FILE_ORIGIN`（文档服务可访问的 Atlas 文件桥源）和 `ATLAS_ONLYOFFICE_SOURCE_IP`（Atlas 文件接口实际看到的文档服务或受控代理地址）。地址只允许管理员设置的内网 IP，文件桥必须只转发 ONLYOFFICE 文件接口，切勿转发工作区口令或公开到外网。Windows 与 WSL 需要额外配置受控桥接；启动本脚本本身不会配置桥或 Atlas 进程环境。文档服务与 Atlas 必须使用不同浏览器来源，避免隔离页面访问工作区凭据。

当前仅支持只读显示，未实现编辑回调及版本保存；尚须在实际部署网络和权限配置下完成端到端验收。不能将项目口令、永久文件地址或客户文件交给公共预览站点，也不能将已打开文件视为财务核准。

社区版文档：https://helpcenter.onlyoffice.com/docs/installation/docs-community-install-docker.aspx
